# Copyright (C) 2025
# Study and Review Management for AnkiConnect
#
# This module handles card studying, reviewing, and scheduling
# functionality for AnkiConnect.

import anki
import anki.utils
from ..utils.deck_helpers import get_direct_child_decks, get_deck_limits, update_parent_deck_silent, assert_card_in_deck

class StudyManager:
    """Manages card studying and review functionality"""
    
    def __init__(self, bridge):
        self.bridge = bridge
    
    def collection(self):
        """Get the current Anki collection"""
        return self.bridge.collection()
    
    def startEditing(self):
        """Start editing session"""
        self.bridge.startEditing()
    
    def stopEditing(self):
        """Stop editing session"""
        self.bridge.stopEditing()

    def refreshGuiIfEnabled(self):
        """Menu-gated modern GUI refresh (delegates to the bridge)."""
        self.bridge.refreshGuiIfEnabled()

    def getNextReviewCard(self, deckName=None, needRender=False):
        """
        Get the next card due for review from a specific deck or all decks.

        Args:
            deckName (str, optional): Name of the deck to get cards from.
            needRender (bool): When True, render the card's question/answer
                HTML via Anki's template engine. Defaults to False because
                rendering is the dominant CPU cost in this call (Tera +
                cloze + JS/MathJax glue) and most polling clients don't
                need it. When False, `question` and `answer` are None.

        Returns:
            dict: Card information if available, None if no cards due.
        """
        collection = self.collection()
        if collection is None:
            return None

        # If deck specified, set it as current. decks.select() persists
        # current_deck_id to the collection config — guard so polling
        # clients don't trigger a DB write on every call.
        if deckName:
            deck = collection.decks.by_name(deckName)
            if deck is None:
                raise Exception(f"Deck '{deckName}' does not exist")
            if collection.decks.selected() != deck['id']:
                collection.decks.select(deck['id'])

        # Fetch top card + its scheduling states in a single backend call.
        # This replaces sched.getCard() + get_scheduling_states() + (later)
        # part of _getAnswerButtons — saving two RPC roundtrips.
        from anki.cards import Card as AnkiCard
        queued = collection._backend.get_queued_cards(fetch_limit=1, intraday_learning_only=False)
        if not queued.cards:
            return None
        queued_card = queued.cards[0]
        card = AnkiCard(collection, backend_card=queued_card.card)
        states = queued_card.states

        # Determine deck IDs for this deck + subdecks (used for
        # lastAnsweredCardId query). deck_and_child_ids is a single backend
        # RPC that returns parent + all descendant ids, replacing an O(N)
        # Python iteration over collection.decks.decks for every call.
        if deckName:
            deck_ids = list(collection.decks.deck_and_child_ids(deck['id']))
        else:
            deck_ids = None  # all decks

        # Find the most recently answered card for this deck scope.
        # Uses INNER JOIN over the subquery form so SQLite can walk the
        # revlog PK index in reverse and stop at the first matching row,
        # instead of filtering every revlog row against an IN-subquery.
        if deck_ids is not None:
            deck_ids_str = ','.join(str(did) for did in deck_ids)
            last_answered = collection.db.scalar(
                f"SELECT r.cid FROM revlog r "
                f"INNER JOIN cards c ON r.cid = c.id "
                f"WHERE c.did IN ({deck_ids_str}) "
                f"ORDER BY r.id DESC LIMIT 1"
            )
        else:
            last_answered = collection.db.scalar(
                "SELECT cid FROM revlog ORDER BY id DESC LIMIT 1"
            )

        # Get card information
        model = card.note_type()
        template = card.template()

        raw_flds = collection.db.scalar("SELECT flds FROM notes WHERE id = ?", card.nid).split("\x1f")
        fields = {info['name']: {'value': raw_flds[info['ord']], 'order': info['ord']} for info in model['flds']}

        # Render question/answer only when the caller asks for them.
        if needRender:
            try:
                question = card.question()
                answer = card.answer()
            except (AttributeError, TypeError):
                try:
                    qa = card._getQA()
                    question = qa['q']
                    answer = qa['a']
                except Exception:
                    question = ""
                    answer = ""
        else:
            question = None
            answer = None
        
        return {
            'cardId': card.id,
            'fields': fields,
            'fieldOrder': card.ord,
            'question': question,
            'answer': answer,
            'modelName': model['name'],
            'templateName': template['name'],
            'deckName': self.bridge.deckNameFromId(card.did),
            'css': model['css'],
            'factor': card.factor,
            'interval': card.ivl,
            'due': card.due,
            'queue': card.queue,
            'type': card.type,
            'noteId': card.nid,
            'buttons': self._getAnswerButtons(card, states=states),
            'flagged': card.flags > 0,
            'lastAnsweredCardId': last_answered
        }
    
    def answerCard(self, cardId, ease, timeTakenSeconds=None, deckName=None):
        """
        Answer a card with the specified ease.

        Uses Anki's grade_now backend RPC, which answers any card by ID
        regardless of its queue position. This is the mechanism Anki's
        browser "Grade Now" feature uses.

        Why not sched.answer_card / sched.answerCard? Anki's Rust backend
        enforces a "top of queue" check inside pop_entry(), reached whenever
        CardAnswer.from_queue is true. The proto-to-Rust conversion at the
        service boundary hardcodes from_queue=true, so every RPC path EXCEPT
        grade_now hits that check. grade_now manually flips from_queue=false
        after conversion, skipping the queue-position requirement entirely.

        Scenarios this fixes:
          1. Cross-day answer: card fetched on day N, answered on day N+1
             after overnight new-card surfacing shuffled the queue.
          2. Rapid "again" streaks: many learning cards churn through the
             queue at short intervals, displacing the target from position 0.

        Earlier workarounds (suspending blockers to push the target to
        position 0) could not converge because get_queued_cards returns a
        bounded batch — suspending cards causes the scheduler to surface
        more from the waiting pool to fill the daily budget.

        Args:
            cardId (int): ID of the card to answer
            ease (int): Ease/difficulty rating (1=Again, 2=Hard, 3=Good, 4=Easy)
            timeTakenSeconds (float, optional): Time taken to answer in seconds.
                                               Defaults to 0 if not provided.
            deckName (str, optional): Parent deck name. If provided, the card must
                                      belong to this deck or one of its subdecks,
                                      otherwise the answer is rejected.

        Returns:
            bool: True if successful
        """
        collection = self.collection()
        if collection is None:
            return False

        if ease < 1 or ease > 4:
            raise Exception(f"Invalid ease value '{ease}'. Must be between 1-4")

        # Cheap existence check + capture deck_id for the optional stats update.
        # collection.getCard() builds a full Card object (note join, template
        # lookup) just to read did; the scalar avoids that.
        deck_id = collection.db.scalar("SELECT did FROM cards WHERE id = ?", cardId)
        if deck_id is None:
            raise Exception(f"Card with ID '{cardId}' does not exist")

        if deckName:
            assert_card_in_deck(collection, cardId, deck_id, deckName)

        from anki.scheduler.v3 import CardAnswer

        rating_map = {
            1: CardAnswer.AGAIN,
            2: CardAnswer.HARD,
            3: CardAnswer.GOOD,
            4: CardAnswer.EASY,
        }

        # grade_now is the only RPC that sets from_queue=false inside the
        # Rust backend, so it skips the "not at top of queue" check in
        # pop_entry(). It still writes the revlog, updates card scheduling
        # state, and increments the deck's new/review daily counters.
        # Runs in its own Rust transaction and emits OpChanges for the
        # op-framework — no startEditing/stopEditing wrap needed.
        collection._backend.grade_now(
            card_ids=[cardId],
            rating=rating_map[ease],
        )

        # grade_now hardcodes milliseconds_taken to 0 in the internal
        # CardAnswer it builds. That zero flows into both the revlog
        # `time` field and the deck's millisecond_delta stat. Patch both
        # if the caller provided a real timing value.
        if timeTakenSeconds is not None:
            time_ms = int(timeTakenSeconds * 1000)
            collection.db.execute(
                "UPDATE revlog SET time = ? WHERE id = ("
                "SELECT id FROM revlog WHERE cid = ? ORDER BY id DESC LIMIT 1"
                ")",
                time_ms, cardId
            )
            collection.sched.update_stats(deck_id, milliseconds_delta=time_ms)

        return True
    
    def resetCard(self, cardId):
        """
        Reset a card to new status
        
        Args:
            cardId (int): ID of the card to reset
            
        Returns:
            bool: True if successful
        """
        collection = self.collection()
        if collection is None:
            return False
            
        try:
            card = collection.getCard(cardId)
            if card is None:
                raise Exception(f"Card with ID '{cardId}' does not exist")
            
            self.startEditing()
            
            # Reset card to new
            collection.sched.forgetCards([cardId])
            collection.autosave()
            
            self.stopEditing()
            return True
            
        except Exception as e:
            self.stopEditing()
            raise e
    
    def forgetCard(self, cardId):
        """
        Forget a card (reset learning progress)
        
        Args:
            cardId (int): ID of the card to forget
            
        Returns:
            bool: True if successful
        """
        collection = self.collection()
        if collection is None:
            return False
            
        try:
            card = collection.getCard(cardId)
            if card is None:
                raise Exception(f"Card with ID '{cardId}' does not exist")
            
            self.startEditing()
            
            # Forget the card
            collection.sched.forgetCards([cardId])
            collection.autosave()
            
            self.stopEditing()
            return True
            
        except Exception as e:
            self.stopEditing()
            raise e
    
    def getDueCards(self, deckName=None, limit=10):
        """
        Get cards that are due for review
        
        Args:
            deckName (str, optional): Name of the deck to get cards from
            limit (int): Maximum number of cards to return
            
        Returns:
            list: List of due card IDs
        """
        collection = self.collection()
        if collection is None:
            return []
        
        # Build query
        if deckName:
            deck = collection.decks.byName(deckName)
            if deck is None:
                raise Exception(f"Deck '{deckName}' does not exist")
            query = f"deck:'{deckName}' is:due"
        else:
            query = "is:due"
        
        # Get due cards
        cardIds = collection.findCards(query)
        return cardIds[:limit] if limit else cardIds
    
    def getNewCards(self, deckName=None, limit=10):
        """
        Get new cards for learning
        
        Args:
            deckName (str, optional): Name of the deck to get cards from
            limit (int): Maximum number of cards to return
            
        Returns:
            list: List of new card IDs
        """
        collection = self.collection()
        if collection is None:
            return []
        
        # Build query
        if deckName:
            deck = collection.decks.byName(deckName)
            if deck is None:
                raise Exception(f"Deck '{deckName}' does not exist")
            query = f"deck:'{deckName}' is:new"
        else:
            query = "is:new"
        
        # Get new cards
        cardIds = collection.findCards(query)
        return cardIds[:limit] if limit else cardIds
    
    def _getAnswerButtons(self, card, states=None):
        """
        Get available answer buttons for a card with timing information

        Args:
            card: Anki card object
            states: Pre-fetched SchedulingStates. If provided, skips the
                extra get_scheduling_states backend call. Pass this when
                the caller already fetched states (e.g. via get_queued_cards).

        Returns:
            list: List of button dictionaries with ease, label, and timing
        """
        collection = self.collection()
        if collection is None:
            return []

        button_labels = {1: "Again", 2: "Hard", 3: "Good", 4: "Easy"}

        try:
            if states is None:
                states = collection._backend.get_scheduling_states(card.id)
            timings = list(collection._backend.describe_next_states(states))
        except Exception:
            timings = ["", "", "", ""]

        return [
            {'ease': i, 'label': button_labels[i], 'timing': timings[i - 1] if i <= len(timings) else ""}
            for i in range(1, 5)
        ]
    
    def getStudyStats(self, deckName=None):
        """
        Get study statistics for a deck or all decks
        
        Args:
            deckName (str, optional): Name of the deck
            
        Returns:
            dict: Study statistics
        """
        collection = self.collection()
        if collection is None:
            return None
            
        if deckName:
            deck = collection.decks.byName(deckName)
            if deck is None:
                raise Exception(f"Deck '{deckName}' does not exist")
            deckId = deck['id']
            deckFilter = f"did = {deckId}"
        else:
            deckFilter = "1=1"  # All decks
        
        # Get counts from database
        newCount = collection.db.scalar(f"select count() from cards where {deckFilter} and queue = 0") or 0
        learningCount = collection.db.scalar(f"select count() from cards where {deckFilter} and queue in (1, 3)") or 0
        reviewCount = collection.db.scalar(f"select count() from cards where {deckFilter} and queue = 2") or 0
        totalCount = collection.db.scalar(f"select count() from cards where {deckFilter}") or 0
        
        # Get cards studied today
        from datetime import datetime, timedelta
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        todayTimestamp = int(today.timestamp() * 1000)
        
        studiedToday = collection.db.scalar(
            f"select count() from revlog where id > ? and cid in (select id from cards where {deckFilter})", 
            todayTimestamp
        ) or 0
        
        return {
            'deckName': deckName or 'All Decks',
            'newCount': newCount,
            'learningCount': learningCount,
            'reviewCount': reviewCount,
            'totalCards': totalCount,
            'studiedToday': studiedToday
        }
    
    def getDeckTimeStats(self, deckName=None, period="allTime"):
        """
        Get time statistics for a deck showing average seconds per card
        
        Args:
            deckName (str, optional): Name of the deck (None for all decks)
            period (str): Time period - "today", "last7days", "last30days", "allTime"
            
        Returns:
            dict: Time statistics including average time per card
        """
        collection = self.collection()
        if collection is None:
            return None
        
        from datetime import datetime, timedelta
        
        # Calculate timestamp based on period
        if period == "today":
            cutoff = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            cutoffTimestamp = int(cutoff.timestamp() * 1000)
            periodDesc = "today"
        elif period == "last7days":
            cutoff = datetime.now() - timedelta(days=7)
            cutoffTimestamp = int(cutoff.timestamp() * 1000)
            periodDesc = "last 7 days"
        elif period == "last30days":
            cutoff = datetime.now() - timedelta(days=30)
            cutoffTimestamp = int(cutoff.timestamp() * 1000)
            periodDesc = "last 30 days"
        else:  # allTime
            cutoffTimestamp = 0
            periodDesc = "all time"
        
        # Build deck filter
        if deckName:
            deck = collection.decks.byName(deckName)
            if deck is None:
                raise Exception(f"Deck '{deckName}' does not exist")
            
            # Get all deck IDs for this deck and its children
            # This includes the parent deck and all nested child decks
            deckIds = [deck['id']]
            for deckId, deckObj in collection.decks.decks.items():
                # Check if this deck is a child of the specified deck
                if deckObj['name'].startswith(deckName + '::'):
                    deckIds.append(int(deckId))
            
            # Create filter for all these decks
            deckIdsStr = ','.join(str(did) for did in deckIds)
            deckFilter = f"and cid in (select id from cards where did in ({deckIdsStr}))"
        else:
            deckFilter = ""
        
        # Query review log for time statistics
        # time column in revlog is in milliseconds
        query = f"""
            select 
                count(*) as review_count,
                sum(time) as total_time_ms,
                avg(time) as avg_time_ms
            from revlog 
            where id > ? {deckFilter}
        """
        
        result = collection.db.first(query, cutoffTimestamp)
        
        if result is None or result[0] == 0:
            return {
                'deckName': deckName or 'All Decks',
                'period': periodDesc,
                'totalReviews': 0,
                'totalTimeSeconds': 0.0,
                'averageTimePerCardSeconds': 0.0
            }
        
        review_count, total_time_ms, avg_time_ms = result
        
        # Convert milliseconds to seconds
        total_time_seconds = (total_time_ms / 1000.0) if total_time_ms else 0.0
        avg_time_seconds = (avg_time_ms / 1000.0) if avg_time_ms else 0.0
        
        return {
            'deckName': deckName or 'All Decks',
            'period': periodDesc,
            'totalReviews': review_count,
            'totalTimeSeconds': round(total_time_seconds, 2),
            'averageTimePerCardSeconds': round(avg_time_seconds, 2)
        }
    
    def setDeckStudyOptions(self, deckName, newCardsPerDay=None, reviewsPerDay=None):
        """
        Set study options for a specific deck only (new cards per day and/or reviews per day)
        This will create a deck-specific config if the deck is currently using a shared config.
        
        If the deck is a child deck (contains '::'), this will also automatically update the
        parent deck's limits to be the sum of all its children's limits.
        
        NOTE: This method does not work with filtered (dynamic) decks, as they use a different
        configuration system stored directly in the deck object.
        
        Args:
            deckName (str): Name of the deck to configure
            newCardsPerDay (int, optional): Maximum new cards to study per day
            reviewsPerDay (int, optional): Maximum review cards per day
            
        Returns:
            dict: Updated configuration with the new settings
        """
        collection = self.collection()
        if collection is None:
            raise Exception("Collection not available")
        
        # Get the deck
        deck = collection.decks.by_name(deckName)
        if deck is None:
            raise Exception(f"Deck '{deckName}' does not exist")
        
        # Check if this is a filtered/dynamic deck
        # Filtered decks have 'dyn' field set to 1 and don't use standard configs
        if deck.get('dyn', 0):
            raise Exception(f"Cannot set study options for filtered deck '{deckName}'. "
                          "Filtered decks use a different configuration system stored in the deck object itself. "
                          "They do not use the standard deck configuration system.")
        
        deck_id = deck['id']
        current_config_id = deck.get('conf', 1)

        # No startEditing()/stopEditing() wrapper (requireReset spam + full UI
        # reset). decks.save() persists directly; GUI refresh gated on the toggle.
        try:
            # Check if this config is shared by other decks. decks_using_config()
            # is Anki's own helper; it returns int DeckIds, so compare against the
            # int deck_id (the old decks.decks scan yielded string keys).
            current_config = collection.decks.get_config(current_config_id)
            is_shared = current_config is not None and any(
                did != deck_id for did in collection.decks.decks_using_config(current_config)
            )

            # If config is shared, clone it for this deck only
            if is_shared:
                new_config_name = f"{deckName} Options"
                new_config_id = collection.decks.add_config_returning_id(new_config_name, current_config)
                
                # Assign the new config to this deck
                deck['conf'] = new_config_id
                collection.decks.save(deck)
                
                # Get the newly created config
                config = collection.decks.get_config(new_config_id)
            else:
                # Config is not shared, safe to modify directly
                config = collection.decks.config_dict_for_deck_id(deck_id)
            
            if config is None:
                raise Exception(f"Could not get configuration for deck '{deckName}'")
            
            # Update new cards per day if specified
            if newCardsPerDay is not None:
                if newCardsPerDay < 0:
                    raise Exception("newCardsPerDay must be >= 0")
                config['new']['perDay'] = newCardsPerDay
            
            # Update reviews per day if specified
            if reviewsPerDay is not None:
                if reviewsPerDay < 0:
                    raise Exception("reviewsPerDay must be >= 0")
                config['rev']['perDay'] = reviewsPerDay
            
            # Save the configuration
            config['mod'] = anki.utils.intTime()
            config['usn'] = collection.usn()
            collection.decks.save(config)

            self.refreshGuiIfEnabled()

            # Store result before parent update
            result = {
                'deckName': deckName,
                'configId': config['id'],
                'configName': config['name'],
                'newCardsPerDay': config['new']['perDay'],
                'reviewsPerDay': config['rev']['perDay'],
                'wasShared': is_shared,
                'createdNewConfig': is_shared
            }
            
            # If this is a child deck, update parent deck's limits to be sum of all children
            if '::' in deckName:
                # Extract parent deck name
                parent_name = deckName.rsplit('::', 1)[0]
                
                # Get all direct children of the parent
                child_decks = get_direct_child_decks(collection, parent_name)
                
                # Calculate sum of all children's limits
                total_new_cards = 0
                total_reviews = 0
                
                for child_deck in child_decks:
                    new_cards, reviews = get_deck_limits(collection, child_deck)
                    total_new_cards += new_cards
                    total_reviews += reviews
                
                # Silently update parent deck with the calculated sums
                update_parent_deck_silent(collection, parent_name, total_new_cards, total_reviews)
            
            return result

        except Exception:
            raise
    
    def extendNewCardLimit(self, deckName, additionalCards):
        """
        Extend today's new card limit for a specific deck using Anki's backend custom study API.
        Uses the same mechanism as Anki's native "Increase today's new card limit" option.
        This properly extends TODAY's limit without permanently modifying deck configuration.
        
        Args:
            deckName (str): Name of the deck
            additionalCards (int): Number of additional new cards to allow today
            
        Returns:
            dict: Information about the extended limit
        """
        collection = self.collection()
        if collection is None:
            raise Exception("Collection not available")
        
        # Get the deck
        deck = collection.decks.by_name(deckName)
        if deck is None:
            raise Exception(f"Deck '{deckName}' does not exist")
        
        if additionalCards <= 0:
            raise Exception("additionalCards must be > 0")
        
        deck_id = deck['id']

        # No startEditing()/stopEditing() wrapper (requireReset spam + full UI
        # reset). custom_study() persists via the Rust backend; refresh on toggle.
        try:
            # Use Anki's backend custom_study API (same as the UI uses)
            from anki.scheduler import CustomStudyRequest
            
            request = CustomStudyRequest()
            request.deck_id = deck_id
            request.new_limit_delta = additionalCards
            
            # This extends the limit using Anki's Rust backend
            collection.sched.custom_study(request)
            
            # Get updated deck to read the extendNew value
            deck = collection.decks.get(deck_id)
            extend_new = deck.get('extendNew', additionalCards)
            
            self.refreshGuiIfEnabled()

            return {
                'deckName': deckName,
                'additionalCardsAllowed': additionalCards,
                'totalExtended': extend_new,
                'message': f"Extended new card limit by {additionalCards} cards for today"
            }

        except Exception:
            raise
    
    def enableStudyForgotten(self, deckName, days=1, filteredDeckName=None):
        """
        Enable studying cards that were forgotten (answered "Again") in the last X days.
        Creates a filtered deck for reviewing forgotten cards.
        Uses Anki's backend custom_study API to match native behavior exactly.
        
        Args:
            deckName (str): Name of the source deck
            days (int): Look back this many days for forgotten cards (default: 1 = today only)
            filteredDeckName (str, optional): Custom name for the filtered deck. If not provided, uses Anki's default.
            
        Returns:
            dict: Information about the created filtered deck
        """
        collection = self.collection()
        if collection is None:
            raise Exception("Collection not available")
        
        # Verify source deck exists
        source_deck = collection.decks.by_name(deckName)
        if source_deck is None:
            raise Exception(f"Deck '{deckName}' does not exist")
        
        if days <= 0:
            raise Exception("days must be > 0")
        
        deck_id = source_deck['id']

        # No startEditing()/stopEditing() wrapper (requireReset spam + full UI
        # reset). custom_study() persists via the Rust backend; refresh on toggle.
        try:
            # Use Anki's backend custom_study API (same as the UI uses)
            from anki.scheduler import CustomStudyRequest
            
            request = CustomStudyRequest()
            request.deck_id = deck_id
            request.forgot_days = days
            
            # This creates the filtered deck using Anki's Rust backend
            collection.sched.custom_study(request)
            
            # Get the created filtered deck (Anki names it "Custom Study Session")
            filtered_deck_name = "Custom Study Session"
            filtered_deck = collection.decks.by_name(filtered_deck_name)
            
            if filtered_deck is None:
                raise Exception("Failed to create custom study deck")
            
            # If custom name requested, rename it
            if filteredDeckName and filteredDeckName != filtered_deck_name:
                filtered_deck['name'] = filteredDeckName
                collection.decks.save(filtered_deck)
                collection.decks.rename(filtered_deck, filteredDeckName)
                filtered_deck_name = filteredDeckName
            
            # Count cards in the filtered deck
            card_count = collection.db.scalar(
                "select count() from cards where did = ?", filtered_deck['id']
            ) or 0
            
            self.refreshGuiIfEnabled()

            return {
                'sourceDeck': deckName,
                'filteredDeckName': filtered_deck_name,
                'filteredDeckId': filtered_deck['id'],
                'days': days,
                'cardsFound': card_count,
                'message': f"Created filtered deck with {card_count} forgotten cards from last {days} day(s)"
            }

        except Exception:
            raise
    
    def createCustomStudy(self, deckName, newCardsPerDay=None, reviewsPerDay=None, 
                         studyForgottenToday=False, extendNewLimit=None):
        """
        Combined API for creating a custom study session with various options.
        
        Args:
            deckName (str): Name of the deck to configure
            newCardsPerDay (int, optional): Set new cards per day limit
            reviewsPerDay (int, optional): Set reviews per day limit
            studyForgottenToday (bool): Create filtered deck for forgotten cards today
            extendNewLimit (int, optional): Extend today's new card limit by this many cards
            
        Returns:
            dict: Results of all requested operations
        """
        results = {
            'deckName': deckName,
            'operations': {}
        }
        
        # Set deck study options if requested
        if newCardsPerDay is not None or reviewsPerDay is not None:
            try:
                config_result = self.setDeckStudyOptions(
                    deckName, newCardsPerDay, reviewsPerDay
                )
                results['operations']['studyOptions'] = {
                    'success': True,
                    'data': config_result
                }
            except Exception as e:
                results['operations']['studyOptions'] = {
                    'success': False,
                    'error': str(e)
                }
        
        # Extend new card limit if requested
        if extendNewLimit is not None:
            try:
                extend_result = self.extendNewCardLimit(deckName, extendNewLimit)
                results['operations']['extendNewLimit'] = {
                    'success': True,
                    'data': extend_result
                }
            except Exception as e:
                results['operations']['extendNewLimit'] = {
                    'success': False,
                    'error': str(e)
                }
        
        # Create forgotten cards filtered deck if requested
        if studyForgottenToday:
            try:
                forgotten_result = self.enableStudyForgotten(deckName, days=1)
                results['operations']['studyForgotten'] = {
                    'success': True,
                    'data': forgotten_result
                }
            except Exception as e:
                results['operations']['studyForgotten'] = {
                    'success': False,
                    'error': str(e)
                }
        
        return results
    
    def undoAnswerCard(self, cardId, deckName):
        """
        Undo the most recent answer for a specific card by card ID.

        This works by:
        1. Optionally validating the card belongs to deckName (or a subdeck)
        2. Finding the most recent revlog entry for the card
        3. Reading the pre-answer state from that entry (lastIvl, prior factor)
        4. Deleting the revlog entry
        5. Restoring the card's scheduling state to what it was before the answer

        After undo, the card will be immediately available in the review queue.

        Args:
            cardId (int): ID of the card whose most recent answer should be undone
            deckName (str, optional): Parent deck name. If provided, the card must
                                      belong to this deck or one of its subdecks,
                                      otherwise the undo is rejected.

        Returns:
            dict: {
                'cardId': int,
                'restoredState': str,   # 'new', 'learning', or 'review'
                'restoredInterval': int  # interval before the answer
            }

        Raises:
            Exception: If card doesn't exist, card not in deck, or no answer history
        """
        collection = self.collection()
        if collection is None:
            raise Exception("Collection not available")

        if cardId is None:
            raise Exception("cardId is required")

        # Verify card exists
        try:
            card = collection.get_card(cardId)
        except Exception:
            raise Exception(f"Card with ID '{cardId}' does not exist")

        # If deckName provided, validate the card belongs to that deck or a subdeck
        if deckName:
            assert_card_in_deck(collection, cardId, card.did, deckName)

        # Find the most recent revlog entry for this card
        revlog_entry = collection.db.first(
            "SELECT id, ease, ivl, lastIvl, factor, time, type "
            "FROM revlog WHERE cid = ? ORDER BY id DESC LIMIT 1",
            cardId
        )

        if revlog_entry is None:
            raise Exception(f"No answer history found for card '{cardId}'")

        revlog_id, ease, ivl, last_ivl, factor, time_taken, review_type = revlog_entry

        # Get the prior factor from the second-most-recent revlog entry (if any)
        prior_entry = collection.db.first(
            "SELECT factor FROM revlog WHERE cid = ? ORDER BY id DESC LIMIT 1 OFFSET 1",
            cardId
        )
        prior_factor = prior_entry[0] if prior_entry and prior_entry[0] else 2500

        # Delete the most recent revlog entry to erase this answer from history
        collection.db.execute("DELETE FROM revlog WHERE id = ?", revlog_id)

        # Restore card state based on the interval before the answer (last_ivl):
        #   last_ivl == 0  → card was new before this answer
        #   last_ivl < 0   → card was in a learning step (value = negative seconds for next step)
        #   last_ivl > 0   → card was a mature review card (value = days)

        # No startEditing()/stopEditing() wrapper here. That legacy pair calls
        # window().requireReset() + maybeReset() — a full Anki UI reset that
        # prints a stack trace on every call (the obsolete path commit 4097403
        # removed from addNote/deleteNote). It only repaints Anki's own Qt
        # windows, which nothing watches on the headless API server; the writes
        # below (the revlog DELETE above, card.flush(), decks.save()) persist
        # directly and sched.reset() refreshes the in-memory counts, so the very
        # next API read returns the undone state. autosave() is a deprecated no-op.

        # ----------------------------------------------------------------
        # Decrement the deck's "shown today" counter for the card type
        # that was active BEFORE the answer. This is the same per-deck
        # counter that Anki's native undo adjusts (stored as
        # deck['newToday'] / deck['lrnToday'] / deck['revToday'], each a
        # [day_number, count] tuple). Without this, the daily budget stays
        # short by 1 even after the revlog entry is removed.
        # ----------------------------------------------------------------
        today = collection.sched.today
        deck = collection.decks.get(card.did)
        if deck:
            if last_ivl == 0:
                today_field = 'newToday'
            elif last_ivl < 0:
                today_field = 'lrnToday'
            else:
                today_field = 'revToday'

            today_entry = deck.get(today_field)
            if today_entry and today_entry[0] == today and today_entry[1] > 0:
                deck[today_field][1] -= 1
                collection.decks.save(deck)

        # ----------------------------------------------------------------
        # Restore the card's scheduling state
        # ----------------------------------------------------------------
        if last_ivl == 0:
            # Card was NEW before this answer.
            # forgetCards() resets to queue=0 via Anki's proper pipeline.
            collection.sched.forgetCards([cardId])

            # forgetCards() assigns due = card.ord (its position number),
            # which buries it deep in the new queue. Re-fetch and set due=0
            # to put it at the FRONT of the new queue so it shows up next.
            card = collection.get_card(cardId)
            card.due = 0
            card.flush()
            restored_state = 'new'

        elif last_ivl < 0:
            # Card was in a LEARNING step — restore to learning queue.
            # Setting due=0 (Unix epoch, year 1970) makes this the most
            # overdue learning card possible, guaranteeing it will be
            # returned first by get_queued_cards() ahead of all other
            # learning, new, and review cards.
            card.type = 1
            card.queue = 1
            card.ivl = 0
            card.due = 0
            card.factor = prior_factor
            card.flush()
            restored_state = 'learning'

        else:
            # Card was a mature REVIEW card — restore to review queue.
            # Setting due=today-999999 (≈2700 years ago) makes this the
            # most overdue review card possible, placing it ahead of all
            # other review cards in the queue. Only intraday learning cards
            # (typically 0–3) can appear before it, so answerCard's
            # batch-peek approach suspends at most a handful of blockers.
            card.type = 2
            card.queue = 2
            card.ivl = last_ivl
            card.due = today - 999999
            card.factor = prior_factor
            card.flush()
            restored_state = 'review'

        # Force the scheduler to recount new/learn/review from the
        # database. Without this the in-memory counts stay stale.
        collection.sched.reset()

        # Optional desktop-GUI repaint. undoAnswerCard does raw DB writes that
        # don't fire the op-framework hooks, so when the Tools-menu toggle
        # "Global Edge: Refresh GUI after API writes" is on (for debugging while
        # watching the Anki GUI) this is the only thing that refreshes the
        # reviewer/deck list. mw.reset() is the modern refresh — no requireReset
        # stack trace. Default off and guarded, so the headless server pays nothing.
        if getattr(self.bridge, 'guiRefreshAfterApiWrites', False):
            try:
                self.bridge.window().reset()
            except Exception:
                pass

        return {
            'cardId': cardId,
            'restoredState': restored_state,
            'restoredInterval': last_ivl
        }

    def getDeckReviewsByDay(self, deckName, days=14):
        """
        Get the number of reviews completed per day for the last N days.
        Uses the same approach as Anki's built-in statistics calendar.
        
        Args:
            deckName (str): Name of the deck (required)
            days (int): Number of days to look back (default: 14)
            
        Returns:
            dict: Daily review statistics with breakdown by card type
        """
        collection = self.collection()
        if collection is None:
            raise Exception("Collection not available")
        
        # Get the deck
        deck = collection.decks.byName(deckName)
        if deck is None:
            raise Exception(f"Deck '{deckName}' does not exist")
        
        deck_id = deck['id']

        # Get all deck IDs (parent + descendants) via the cached backend RPC.
        # Replaces an O(total_decks) Python iteration over collection.decks.decks.
        deck_ids = list(collection.decks.deck_and_child_ids(deck_id))
        deck_ids_str = ','.join(str(did) for did in deck_ids)

        # Get day cutoff from scheduler (this is when "today" ENDS, typically 4am tomorrow)
        day_cutoff = collection.sched.day_cutoff

        # Calculate start of "today" in Anki's system (24 hours before day_cutoff)
        day_start = day_cutoff - 86400

        # Calculate timestamp for N days ago
        cutoff_timestamp = (day_cutoff - (days * 86400)) * 1000

        # Query revlog using INNER JOIN onto cards. Replaces the IN-subquery
        # form (cid IN (SELECT id FROM cards WHERE did IN (...))) so SQLite
        # can walk the revlog primary-key index from the cutoff forward and
        # join into cards by id, instead of materializing the cards subquery
        # and probing per revlog row.
        query = f"""
            SELECT
                cast((r.id/1000.0 - ?) / 86400.0 as int) as day,
                sum(case when r.type = 0 then 1 else 0 end) as learning,
                sum(case when r.type = 1 then 1 else 0 end) as review,
                sum(case when r.type = 2 then 1 else 0 end) as relearn,
                sum(case when r.type = 3 then 1 else 0 end) as filtered,
                count(*) as total
            FROM revlog r
            INNER JOIN cards c ON r.cid = c.id
            WHERE r.id > ?
                AND c.did IN ({deck_ids_str})
            GROUP BY day
            ORDER BY day
        """

        results = collection.db.all(query, day_cutoff, cutoff_timestamp)
        
        # Process results into a more readable format
        from datetime import datetime, timedelta
        
        stats = []
        total_reviews = 0
        
        for row in results:
            day_offset, learning, review, relearn, filtered, total = row
            
            # Calculate actual date using day_start (not day_cutoff) to get correct calendar dates
            date_timestamp = day_start + (day_offset * 86400)
            date = datetime.fromtimestamp(date_timestamp).strftime('%Y-%m-%d')
            
            stats.append({
                'date': date,
                'dayNumber': day_offset,
                'learning': learning,
                'review': review,
                'relearn': relearn,
                'filtered': filtered,
                'total': total
            })
            
            total_reviews += total
        
        # Calculate average
        avg_per_day = round(total_reviews / days, 1) if days > 0 else 0.0
        
        # Get current count of new cards available in the deck
        new_cards_available = collection.db.scalar(
            f"SELECT COUNT(*) FROM cards WHERE did IN ({deck_ids_str}) AND queue = 0"
        ) or 0
        
        return {
            'deckName': deckName,
            'days': days,
            'stats': stats,
            'totalReviews': total_reviews,
            'averagePerDay': avg_per_day,
            'newCardsAvailable': new_cards_available
        }

    def getDeckReviewsByDayMulti(self, deckNames, days=14):
        """
        Batched version of getDeckReviewsByDay across many decks.

        Replaces N callers each running 2 SQLite queries with one revlog
        query and one new-cards query for the whole batch, then rolls up
        per-deck in Python. Designed for callers that previously fanned
        out via callAnkiMulti.

        Missing decks are returned with empty stats (no exception) so a
        single bad deck doesn't fail the batch.

        Args:
            deckNames (list[str]): Deck names to fetch stats for.
            days (int): Number of days to look back (default: 14).

        Returns:
            list[dict]: Results in the same order as deckNames, each
            with the same shape as getDeckReviewsByDay.
        """
        from datetime import datetime

        collection = self.collection()
        if collection is None:
            raise Exception("Collection not available")

        if not deckNames:
            return []

        # Resolve each requested deck to its descendant did set, and build
        # a child_did -> requested deckName map for rollup. If two requested
        # decks overlap (one is an ancestor of another), the later one wins
        # for the overlap; overlapping inputs are not a supported pattern.
        child_to_parent = {}
        child_did_to_suffix = {}  # did -> child deck suffix e.g. "Text", "Audio"
        all_dids = set()
        for deckName in deckNames:
            deck = collection.decks.by_name(deckName)
            if deck is None:
                continue
            descendant_dids = collection.decks.deck_and_child_ids(deck['id'])
            for did in descendant_dids:
                child_to_parent[did] = deckName
                all_dids.add(did)
                child_deck = collection.decks.get(did)
                if child_deck is not None:
                    child_name = child_deck['name']
                    if '::' in child_name:
                        child_did_to_suffix[did] = child_name.split('::')[-1]

        empty_result = lambda dn: {
            'deckName': dn,
            'days': days,
            'stats': [],
            'totalReviews': 0,
            'averagePerDay': 0.0,
            'newCardsAvailable': 0,
            'newCardsAvailableText': 0,
            'newCardsAvailableAudio': 0,
        }

        if not all_dids:
            return [empty_result(dn) for dn in deckNames]

        day_cutoff = collection.sched.day_cutoff
        day_start = day_cutoff - 86400
        cutoff_timestamp = (day_cutoff - (days * 86400)) * 1000
        all_dids_str = ','.join(str(d) for d in all_dids)

        # One revlog scan for the whole batch, grouped by deck and day.
        # INNER JOIN into cards keeps the revlog PK scan from cutoff
        # forward instead of materializing an IN-subquery.
        revlog_query = f"""
            SELECT
                c.did as did,
                cast((r.id/1000.0 - ?) / 86400.0 as int) as day,
                sum(case when r.type = 0 then 1 else 0 end) as learning,
                sum(case when r.type = 1 then 1 else 0 end) as review,
                sum(case when r.type = 2 then 1 else 0 end) as relearn,
                sum(case when r.type = 3 then 1 else 0 end) as filtered,
                count(*) as total
            FROM revlog r
            INNER JOIN cards c ON r.cid = c.id
            WHERE r.id > ?
                AND c.did IN ({all_dids_str})
            GROUP BY c.did, day
        """
        revlog_rows = collection.db.all(revlog_query, day_cutoff, cutoff_timestamp)

        # One new-cards count for the whole batch, grouped by deck.
        new_cards_query = f"""
            SELECT did, COUNT(*)
            FROM cards
            WHERE did IN ({all_dids_str}) AND queue = 0
            GROUP BY did
        """
        new_cards_rows = collection.db.all(new_cards_query)

        # Roll up to requested parent decks, and also track per-child-deck counts.
        new_cards_by_parent = {dn: 0 for dn in deckNames}
        new_cards_by_child_deck = {dn: {} for dn in deckNames}  # deckName -> {suffix: count}
        for did, cnt in new_cards_rows:
            parent = child_to_parent.get(did)
            if parent is not None:
                new_cards_by_parent[parent] += cnt
                suffix = child_did_to_suffix.get(did)
                if suffix is not None:
                    child_counts = new_cards_by_child_deck[parent]
                    child_counts[suffix] = child_counts.get(suffix, 0) + cnt

        # parent -> {day_offset: {learning, review, relearn, filtered, total}}
        stats_by_parent_day = {dn: {} for dn in deckNames}
        for row in revlog_rows:
            did, day_offset, learning, review, relearn, filtered, total = row
            parent = child_to_parent.get(did)
            if parent is None:
                continue
            bucket = stats_by_parent_day[parent].setdefault(day_offset, {
                'learning': 0, 'review': 0, 'relearn': 0, 'filtered': 0, 'total': 0,
            })
            bucket['learning'] += learning
            bucket['review'] += review
            bucket['relearn'] += relearn
            bucket['filtered'] += filtered
            bucket['total'] += total

        results = []
        for deckName in deckNames:
            day_buckets = stats_by_parent_day.get(deckName, {})
            stats = []
            total_reviews = 0
            for day_offset in sorted(day_buckets.keys()):
                b = day_buckets[day_offset]
                date_timestamp = day_start + (day_offset * 86400)
                date = datetime.fromtimestamp(date_timestamp).strftime('%Y-%m-%d')
                stats.append({
                    'date': date,
                    'dayNumber': day_offset,
                    'learning': b['learning'],
                    'review': b['review'],
                    'relearn': b['relearn'],
                    'filtered': b['filtered'],
                    'total': b['total'],
                })
                total_reviews += b['total']

            avg_per_day = round(total_reviews / days, 1) if days > 0 else 0.0
            child_counts = new_cards_by_child_deck.get(deckName, {})
            results.append({
                'deckName': deckName,
                'days': days,
                'stats': stats,
                'totalReviews': total_reviews,
                'averagePerDay': avg_per_day,
                'newCardsAvailable': new_cards_by_parent.get(deckName, 0),
                'newCardsAvailableText': child_counts.get('Text', 0),
                'newCardsAvailableAudio': child_counts.get('Audio', 0),
            })

        return results
