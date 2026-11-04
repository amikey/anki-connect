# Copyright (C) 2016 Alex Yatskov <alex@foosoft.net>
# Author: Alex Yatskov <alex@foosoft.net>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.


import anki
import aqt
import base64
import hashlib
import inspect
import json
import os
import os.path
import re
import sys
import tempfile
from time import time
from unicodedata import normalize
from operator import itemgetter

try:
    from .managers import NoteManager, StudyManager
except ImportError:
    # Fallback for older Python versions or different import contexts
    try:
        from managers import NoteManager, StudyManager
    except ImportError:
        # Final fallback for legacy structure
        import note_manager
        import study_manager
        NoteManager = note_manager.NoteManager
        StudyManager = study_manager.StudyManager

try:
    from .utils import (
        makeBytes, makeStr, download, verifyString, verifyStringList,
        getMimeType, audioInject, AjaxServer, ConfigManager, assert_card_in_deck
    )
except ImportError:
    # Fallback for older Python versions or different import contexts
    try:
        from utils import (
            makeBytes, makeStr, download, verifyString, verifyStringList,
            getMimeType, audioInject, AjaxServer, ConfigManager, assert_card_in_deck
        )
    except ImportError:
        # Final fallback - define locally
        from utils.helpers import makeBytes, makeStr, download, verifyString, verifyStringList, getMimeType, audioInject
        from utils.network import AjaxServer
        from utils.config_manager import ConfigManager
        from utils.deck_helpers import assert_card_in_deck



#
# Constants
#

API_VERSION = 5
ADDON_VERSION = "0.1.19"  # This will be auto-updated by build_zip.sh
TICK_INTERVAL = int(os.getenv('ANKICONNECT_TICK_INTERVAL_MS', '5'))

# Persisted-config key for the Tools-menu "refresh GUI after API writes" toggle.
CONFIG_GUI_REFRESH = 'guiRefreshAfterApiWrites'
URL_TIMEOUT = 10
URL_UPGRADE = 'https://raw.githubusercontent.com/FooSoft/anki-connect/master/AnkiConnect.py'
NET_ADDRESS = os.getenv('ANKICONNECT_BIND_ADDRESS', '127.0.0.1')
NET_BACKLOG = 5
NET_PORT = 8765


#
# General helpers
#

if sys.version_info[0] < 3:
    import urllib2
    web = urllib2

    from PyQt4.QtCore import QTimer
    from PyQt4.QtGui import QMessageBox
else:
    unicode = str

    from urllib import request
    web = request

    # Try PyQt6 first (for newer Anki versions), then fall back to PyQt5
    try:
        from PyQt6.QtCore import QTimer
        from PyQt6.QtWidgets import QMessageBox
    except ImportError:
        from PyQt5.QtCore import QTimer
        from PyQt5.QtWidgets import QMessageBox


#
# Helpers
#

def webApi(*versions):
    """Decorator for marking API methods"""
    def decorator(func):
        method = lambda *args, **kwargs: func(*args, **kwargs)
        setattr(method, 'versions', versions)
        setattr(method, 'api', True)
        return method
    return decorator


def parseMultipartData(body, boundary):
    """
    Parse multipart/form-data request body
    
    Args:
        body: Request body as bytes
        boundary: Boundary string from Content-Type header
        
    Returns:
        dict: Parsed fields and files {'fields': {name: value}, 'files': {name: (filename, data)}}
    """
    parts = body.split(makeBytes('--' + boundary))
    fields = {}
    files = {}
    
    for part in parts:
        if len(part) <= 2 or part == makeBytes('--\r\n') or part == makeBytes('--'):
            continue
            
        # Split headers and content
        if makeBytes('\r\n\r\n') in part:
            header_part, content = part.split(makeBytes('\r\n\r\n'), 1)
        else:
            continue
        
        # Remove trailing \r\n from content
        if content.endswith(makeBytes('\r\n')):
            content = content[:-2]
        
        # Parse Content-Disposition header
        headers = header_part.split(makeBytes('\r\n'))
        content_disposition = None
        for header in headers:
            if header.lower().startswith(makeBytes('content-disposition:')):
                content_disposition = makeStr(header)
                break
        
        if not content_disposition:
            continue
        
        # Extract name and filename
        name_match = re.search(r'name="([^"]+)"', content_disposition)
        filename_match = re.search(r'filename="([^"]+)"', content_disposition)
        
        if name_match:
            name = name_match.group(1)
            if filename_match:
                # This is a file
                filename = filename_match.group(1)
                files[name] = (filename, content)
            else:
                # This is a regular field
                fields[name] = makeStr(content)
    
    return {'fields': fields, 'files': files}


#
# AnkiNoteParams
#

class Audio:
    """Audio spec for one note's field(s).

    Module-level ON PURPOSE. This used to be defined inside
    AnkiNoteParams.__init__, which built a brand-new class object on EVERY
    AnkiNoteParams(...) — i.e. every addNote/addAudioNote/updateNoteFields call.
    Each class creation makes a `type` plus its methods (function), the
    __dict__/__weakref__ getset_descriptors, the class dict and __mro__ tuple
    (~8 objects), and a class is a self-referential cycle (Audio <-> __mro__).
    Anki runs gc.disable(), so every one leaked permanently — the 8 obj/call
    addNote leak the probe found. Defined once here, it's only instantiated per
    call (cheap, no cycle). See INCIDENT.md."""
    def __init__(self, params):
        self.url = params.get('url')
        self.filename = params.get('filename')
        self.skipHash = params.get('skipHash')
        self.fields = params.get('fields', [])

    def validate(self):
        return (
            verifyString(self.url) and
            verifyString(self.filename) and os.path.dirname(self.filename) == '' and
            verifyStringList(self.fields) and
            (verifyString(self.skipHash) or self.skipHash is None)
        )


class AnkiNoteParams:
    def __init__(self, params):
        self.deckName = params.get('deckName')
        self.modelName = params.get('modelName')
        self.fields = params.get('fields', {})
        self.tags = params.get('tags', [])

        audio = Audio(params.get('audio', {}))
        self.audio = audio if audio.validate() else None


    def validate(self):
        return (
            verifyString(self.deckName) and
            verifyString(self.modelName) and
            type(self.fields) == dict and verifyStringList(list(self.fields.keys())) and verifyStringList(list(self.fields.values())) and
            type(self.tags) == list and verifyStringList(self.tags)
        )


#
# AnkiBridge
#

class AnkiBridge:
    def __init__(self):
        # Default off; AnkiConnect._setupSettingsMenu() loads the persisted
        # value at startup and the Tools-menu toggle flips it. undoAnswerCard
        # reads this to decide whether to call mw.reset() after its raw writes.
        self.guiRefreshAfterApiWrites = False
    
    def storeMediaFile(self, filename, data):
        self.deleteMediaFile(filename)
        self.media().writeData(filename, base64.b64decode(data))


    def retrieveMediaFile(self, filename):
        # based on writeData from anki/media.py
        filename = os.path.basename(filename)
        filename = normalize("NFC", filename)
        filename = self.media().stripIllegal(filename)

        path = os.path.join(self.media().dir(), filename)
        if os.path.exists(path):
            with open(path, 'rb') as file:
                return base64.b64encode(file.read()).decode('ascii')

        return False


    def deleteMediaFile(self, filename):
        self.media().syncDelete(filename)


    def addNote(self, params):
        collection = self.collection()
        if collection is None:
            return

        note = self.createNote(params)
        if note is None:
            return

        # Get the target deck
        deck = collection.decks.by_name(params.deckName)
        if deck is None:
            return

        if params.audio is not None and len(params.audio.fields) > 0:
            data = download(params.audio.url)
            if data is not None:
                if params.audio.skipHash is None:
                    skip = False
                else:
                    m = hashlib.md5()
                    m.update(data)
                    skip = params.audio.skipHash == m.hexdigest()

                if not skip:
                    audioInject(note, params.audio.fields, params.audio.filename)
                    self.media().writeData(params.audio.filename, data)

        # Modern Anki (2.1.50+): collection.add_note() runs inside a Rust
        # transaction and fires the note_will_be_added hook + UI ops itself.
        # The legacy startEditing/stopEditing wrap calls aqt.mw.requireReset()
        # which is obsolete — it prints a stack trace and forces a full UI
        # reset on every call. Skip it here for latency.
        try:
            collection.add_note(note, deck['id'])
        except (AttributeError, TypeError):
            # Older Anki: keep the legacy path including UI reset wrapper.
            self.startEditing()
            try:
                collection.addNote(note)
                cardIds = [card.id for card in note.cards()]
                if cardIds:
                    self.changeDeck(cardIds, params.deckName)
            finally:
                self.stopEditing()

        return note.id


    def canAddNote(self, note):
        return bool(self.createNote(note))


    def createNote(self, params):
        collection = self.collection()
        if collection is None:
            return

        model = collection.models.by_name(params.modelName)
        if model is None:
            return

        deck = collection.decks.by_name(params.deckName)
        if deck is None:
            return

        note = anki.notes.Note(collection, model)
        note.tags = params.tags

        for name, value in params.fields.items():
            if name in note:
                note[name] = value

        # Check for duplicates - Anki checks the first field by default
        # Return the note even if it's a duplicate - let the caller decide
        return note

    def updateNoteFields(self, params):
        """
        Update note fields with optional audio download and deck validation
        
        Args:
            params (dict): {
                'id': noteId (required),
                'deckName': parent deck name (required),
                'fields': {fieldName: value, ...} (optional - text fields),
                'audioFields': {fieldName: audioUrl, ...} (optional - audio from URLs)
            }
            
        Returns:
            dict: Updated note information
        """
        collection = self.collection()
        if collection is None:
            raise Exception("Collection not available")
        
        # Validate deck name is provided and not empty
        deck_name = params.get('deckName')
        if deck_name is None:
            raise Exception("Deck name is required")
        if not deck_name or deck_name.strip() == '':
            raise Exception("Deck name cannot be empty")
        
        # Validate note ID
        note_id = params.get('id')
        if note_id is None:
            raise Exception("Note ID is required")
        
        # Get the note
        try:
            note = collection.get_note(note_id)
        except Exception as e:
            raise Exception(f"Failed to get note with ID {note_id}: {str(e)}")
        
        # Validate that at least ONE card belongs to the specified deck or subdeck
        cards = note.cards()
        if not cards:
            raise Exception(f"Note {note_id} has no cards")
        
        has_match = False
        
        for card in cards:
            card_deck_name = collection.decks.get(card.did)['name']
            
            # Case-sensitive check: exact match or subdeck
            if card_deck_name == deck_name or card_deck_name.startswith(deck_name + '::'):
                has_match = True
                break  # Found a match, exit immediately
        
        if not has_match:
            raise Exception(
                f"Note {note_id} does not belong to deck '{deck_name}' or its subdecks"
            )
        
        # Update text fields
        fields = params.get('fields', {})
        for name, value in fields.items():
            if name in note:
                note[name] = value
            else:
                raise Exception(f"Field '{name}' not found in note type '{note.note_type()['name']}'")
        
        # Update audio fields
        audio_fields = params.get('audioFields', {})
        for field_name, audio_url in audio_fields.items():
            if field_name not in note:
                raise Exception(f"Field '{field_name}' not found in note type '{note.note_type()['name']}'")
            
            if not audio_url or not verifyString(audio_url):
                raise Exception(f"Invalid audio URL for field '{field_name}'")
            
            try:
                # Download audio file from URL
                audio_data = download(audio_url)
                if audio_data is None or len(audio_data) == 0:
                    raise Exception(f"Failed to download audio from URL: {audio_url}")
                
                # Generate unique filename from URL
                import urllib.parse
                parsed_url = urllib.parse.urlparse(audio_url)
                url_filename = os.path.basename(parsed_url.path)
                
                if url_filename and '.' in url_filename:
                    # Use filename from URL with unique timestamp
                    timestamp = int(time())
                    name, ext = os.path.splitext(url_filename)
                    audio_filename = f"{name}_{timestamp}{ext}"
                else:
                    # Generate completely new filename
                    timestamp = int(time())
                    url_hash = hashlib.md5(audio_url.encode('utf-8')).hexdigest()[:8]
                    audio_filename = f"audio_{timestamp}_{url_hash}.mp3"
                
                # Ensure filename has no directory components
                audio_filename = os.path.basename(audio_filename)
                
                # Store audio file in media folder
                self.media().writeData(audio_filename, audio_data)
                
                # Replace field content with audio reference
                note[field_name] = f'[sound:{audio_filename}]'
                
            except Exception as e:
                raise Exception(f"Failed to process audio for field '{field_name}': {str(e)}")
        
        # Validate that at least one field was provided
        if not fields and not audio_fields:
            raise Exception("No fields or audioFields provided to update")
        
        # Save note using modern API (creates undo entry + fires op hooks).
        # No startEditing()/stopEditing() wrapper: requireReset() prints a stack
        # trace + a full UI reset per call. update_note() persists directly;
        # autosave() is a deprecated no-op. GUI refresh gated on the menu toggle.
        collection.update_note(note)
        self.refreshGuiIfEnabled()

        # Return updated note information
        return {
            'noteId': note.id,
            'fields': {name: note[name] for name in note.keys()},
            'tags': note.tags,
            'modelName': note.note_type()['name'],
            'cards': [card.id for card in note.cards()]
        }

    def addTags(self, notes, tags, add=True):
        self.startEditing()
        self.collection().tags.bulkAdd(notes, tags, add)
        self.stopEditing()


    def getTags(self):
        return self.collection().tags.all()


    def suspend(self, cards, suspend=True):
        for card in cards:
            isSuspended = self.isSuspended(card)
            if suspend and isSuspended:
                cards.remove(card)
            elif not suspend and not isSuspended:
                cards.remove(card)

        if cards:
            self.startEditing()
            if suspend:
                self.collection().sched.suspendCards(cards)
            else:
                self.collection().sched.unsuspendCards(cards)
            self.stopEditing()
            return True

        return False


    def areSuspended(self, cards):
        suspended = []
        for card in cards:
            card = self.collection().getCard(card)
            if card.queue == -1:
                suspended.append(True)
            else:
                suspended.append(False)
        return suspended


    def flagCard(self, cardId, deckName=None):
        """Flag a card with red flag (flag value 1); with deckName, only a card in that deck or a subdeck"""
        collection = self.collection()
        if collection is None:
            return False

        card = collection.get_card(cardId)
        if card is None:
            raise Exception(f"Card with ID '{cardId}' does not exist")
        if deckName:
            assert_card_in_deck(collection, cardId, card.did, deckName)

        # No startEditing()/stopEditing() wrapper: requireReset() prints a stack
        # trace and forces a full UI reset on EVERY call (the obsolete path
        # commit 4097403 removed from addNote; it was this endpoint's 4/s crawl).
        # card.flush() persists the flag directly; autosave() is a deprecated
        # no-op. Mirrors undoAnswerCard.
        card.flags = 1  # Red flag
        card.flush()
        return True


    def unflagCard(self, cardId, deckName=None):
        """Remove flag from a card (set flag value to 0); with deckName, only a card in that deck or a subdeck"""
        collection = self.collection()
        if collection is None:
            return False

        card = collection.get_card(cardId)
        if card is None:
            raise Exception(f"Card with ID '{cardId}' does not exist")
        if deckName:
            assert_card_in_deck(collection, cardId, card.did, deckName)

        # No startEditing()/stopEditing() wrapper — see flagCard. card.flush()
        # persists directly; the legacy wrapper only spammed requireReset().
        card.flags = 0  # No flag
        card.flush()
        return True


    def isCardFlagged(self, cardId):
        """Check if a card is flagged"""
        collection = self.collection()
        if collection is None:
            return False
        
        try:
            card = collection.getCard(cardId)
            if card is None:
                raise Exception(f"Card with ID '{cardId}' does not exist")
            return card.flags > 0
        except Exception as e:
            raise e


    def areDue(self, cards):
        due = []
        for card in cards:
            if self.findCards('cid:%s is:new' % card):
                due.append(True)
                continue

            date, ivl = self.collection().db.all('select id/1000.0, ivl from revlog where cid = ?', card)[-1]
            if (ivl >= -1200):
                if self.findCards('cid:%s is:due' % card):
                    due.append(True)
                else:
                    due.append(False)
            else:
                if date - ivl <= time():
                    due.append(True)
                else:
                    due.append(False)

        return due


    def getIntervals(self, cards, complete=False):
        intervals = []
        for card in cards:
            if self.findCards('cid:%s is:new' % card):
                intervals.append(0)
                continue

            interval = self.collection().db.list('select ivl from revlog where cid = ?', card)
            if not complete:
                interval = interval[-1]
            intervals.append(interval)
        return intervals


    def startEditing(self):
        self.window().requireReset()


    def stopEditing(self):
        if self.collection() is not None:
            self.window().maybeReset()


    def window(self):
        return aqt.mw


    def refreshGuiIfEnabled(self):
        """Modern, opt-in replacement for the legacy startEditing()/stopEditing()
        wrapper. Repaints the Anki desktop UI via mw.reset() ONLY when the
        Tools-menu 'Refresh GUI after API writes' toggle is on (default off, so
        the headless server does no per-write UI work). Unlike requireReset() it
        prints no stack trace; unlike maybeReset() (now a no-op) it actually
        refreshes. Guarded — a refresh failure never fails the write."""
        if getattr(self, 'guiRefreshAfterApiWrites', False):
            try:
                self.window().reset()
            except Exception:
                pass


    def reviewer(self):
        return self.window().reviewer


    def collection(self):
        return self.window().col


    def scheduler(self):
        return self.collection().sched


    def multi(self, actions):
        response = []
        for item in actions:
            response.append(AnkiConnect.handler(ac, item))
        return response


    def media(self):
        collection = self.collection()
        if collection is not None:
            return collection.media


    def modelNames(self):
        collection = self.collection()
        if collection is not None:
            return collection.models.allNames()


    def modelNamesAndIds(self):
        models = {}

        modelNames = self.modelNames()
        for model in modelNames:
            mid = self.collection().models.by_name(model)['id']
            mid = int(mid)  # sometimes Anki stores the ID as a string
            models[model] = mid

        return models


    def modelNameFromId(self, modelId):
        collection = self.collection()
        if collection is not None:
            model = collection.models.get(modelId)
            if model is not None:
                return model['name']


    def modelFieldNames(self, modelName):
        collection = self.collection()
        if collection is not None:
            model = collection.models.byName(modelName)
            if model is not None:
                return [field['name'] for field in model['flds']]


    def modelFieldsOnTemplates(self, modelName):
        model = self.collection().models.byName(modelName)

        if model is not None:
            templates = {}
            for template in model['tmpls']:
                fields = []

                for side in ['qfmt', 'afmt']:
                    fieldsForSide = []

                    # based on _fieldsOnTemplate from aqt/clayout.py
                    matches = re.findall('{{[^#/}]+?}}', template[side])
                    for match in matches:
                        # remove braces and modifiers
                        match = re.sub(r'[{}]', '', match)
                        match = match.split(":")[-1]

                        # for the answer side, ignore fields present on the question side + the FrontSide field
                        if match == 'FrontSide' or side == 'afmt' and match in fields[0]:
                            continue
                        fieldsForSide.append(match)


                    fields.append(fieldsForSide)

                templates[template['name']] = fields

            return templates




    def getDeckConfig(self, deck):
        if not deck in self.deckNames():
            return False

        did = self.collection().decks.id(deck)
        return self.collection().decks.confForDid(did)


    def saveDeckConfig(self, config):
        configId = str(config['id'])
        if not configId in self.collection().decks.dconf:
            return False

        mod = anki.utils.intTime()
        usn = self.collection().usn()

        config['mod'] = mod
        config['usn'] = usn

        self.collection().decks.dconf[configId] = config
        self.collection().decks.changed = True
        return True


    def setDeckConfigId(self, decks, configId):
        for deck in decks:
            if not deck in self.deckNames():
                return False

        if not str(configId) in self.collection().decks.dconf:
            return False

        for deck in decks:
            did = str(self.collection().decks.id(deck))
            aqt.mw.col.decks.decks[did]['conf'] = configId

        return True


    def cloneDeckConfigId(self, name, cloneFrom=1):
        if not str(cloneFrom) in self.collection().decks.dconf:
            return False

        cloneFrom = self.collection().decks.getConf(cloneFrom)
        return self.collection().decks.confId(name, cloneFrom)


    def removeDeckConfigId(self, configId):
        if configId == 1 or not str(configId) in self.collection().decks.dconf:
            return False

        self.collection().decks.remConf(configId)
        return True


    def deckNames(self):
        collection = self.collection()
        if collection is not None:
            return collection.decks.allNames()


    def deckNamesAndIds(self):
        decks = {}

        deckNames = self.deckNames()
        for deck in deckNames:
            did = self.collection().decks.id(deck)
            decks[deck] = did

        return decks


    def deckNameFromId(self, deckId):
        collection = self.collection()
        if collection is not None:
            deck = collection.decks.get(deckId)
            if deck is not None:
                return deck['name']


    def findNotes(self, query=None):
        if query is not None:
            return self.collection().findNotes(query)
        else:
            return []


    def findCards(self, query=None):
        if query is not None:
            return self.collection().findCards(query)
        else:
            return []

    def cardsInfo(self,cards):
        result = []
        for cid in cards:
            try:
                card = self.collection().getCard(cid)
                model = card.model()
                note = card.note()
                fields = {}
                for info in model['flds']:
                    order = info['ord']
                    name = info['name']
                    fields[name] = {'value': note.fields[order], 'order': order}
                
                # Get question and answer with version compatibility
                try:
                    # Try new Anki 2.1.50+ API
                    question = card.question()
                    answer = card.answer()
                except (AttributeError, TypeError):
                    # Fall back to older API
                    try:
                        qa = card._getQA()
                        question = qa['q']
                        answer = qa['a']
                    except:
                        question = ""
                        answer = ""
            
                result.append({
                    'cardId': card.id,
                    'fields': fields,
                    'fieldOrder': card.ord,
                    'question': question,
                    'answer': answer,
                    'modelName': model['name'],
                    'deckName': self.deckNameFromId(card.did),
                    'css': model['css'],
                    'factor': card.factor, 
                    #This factor is 10 times the ease percentage, 
                    # so an ease of 310% would be reported as 3100
                    'interval': card.ivl,
                    'note': card.nid,
                    'flagged': card.flags > 0
                })
            except TypeError as e:
                # Anki will give a TypeError if the card ID does not exist.
                # Best behavior is probably to add an "empty card" to the
                # returned result, so that the items of the input and return
                # lists correspond.
                result.append({})

        return result

    def notesInfo(self,notes):
        result = []
        for nid in notes:
            try:
                note = self.collection().get_note(nid)
                model = note.note_type()

                fields = {}
                for info in model['flds']:
                    order = info['ord']
                    name = info['name']
                    fields[name] = {'value': note.fields[order], 'order': order}
            
                result.append({
                    'noteId': note.id,
                    'tags' : note.tags,
                    'fields': fields,
                    'modelName': model['name'],
                    'cards': self.collection().db.list(
                        "select id from cards where nid = ? order by ord", note.id)
                })
            except TypeError as e:
                # Anki will give a TypeError if the note ID does not exist.
                # Best behavior is probably to add an "empty card" to the
                # returned result, so that the items of the input and return
                # lists correspond.
                result.append({})
        return result


    def getDecks(self, cards):
        decks = {}
        for card in cards:
            did = self.collection().db.scalar('select did from cards where id = ?', card)
            deck = self.collection().decks.get(did)['name']

            if deck in decks:
                decks[deck].append(card)
            else:
                decks[deck] = [card]

        return decks


    def changeDeck(self, cards, deck):
        self.startEditing()

        did = self.collection().decks.id(deck)
        mod = anki.utils.intTime()
        usn = self.collection().usn()

        # normal cards
        scids = anki.utils.ids2str(cards)
        # remove any cards from filtered deck first
        self.collection().sched.remFromDyn(cards)

        # then move into new deck
        self.collection().db.execute('update cards set usn=?, mod=?, did=? where id in ' + scids, usn, mod, did)
        self.stopEditing()


    def deleteDecks(self, decks, cardsToo=False):
        self.startEditing()
        for deck in decks:
            did = self.collection().decks.id(deck)
            self.collection().decks.rem(did, cardsToo)
        self.stopEditing()


    def cardsToNotes(self, cards):
        return self.collection().db.list('select distinct nid from cards where id in ' + anki.utils.ids2str(cards))


    def guiBrowse(self, query=None):
        browser = aqt.dialogs.open('Browser', self.window())
        browser.activateWindow()

        if query is not None:
            browser.form.searchEdit.lineEdit().setText(query)
            if hasattr(browser, 'onSearch'):
                browser.onSearch()
            else:
                browser.onSearchActivated()

        return browser.model.cards


    def guiAddCards(self):
        addCards = aqt.dialogs.open('AddCards', self.window())
        addCards.activateWindow()


    def guiReviewActive(self):
        return self.reviewer().card is not None and self.window().state == 'review'


    def guiCurrentCard(self):
        if not self.guiReviewActive():
            return

        reviewer = self.reviewer()
        card = reviewer.card
        model = card.model()
        note = card.note()

        fields = {}
        for info in model['flds']:
            order = info['ord']
            name = info['name']
            fields[name] = {'value': note.fields[order], 'order': order}

        # Get question and answer with version compatibility
        try:
            # Try new Anki 2.1.50+ API
            question = card.question()
            answer = card.answer()
        except (AttributeError, TypeError):
            # Fall back to older API
            try:
                qa = card._getQA()
                question = qa['q']
                answer = qa['a']
            except:
                question = ""
                answer = ""

        if card is not None:
            return {
                'cardId': card.id,
                'fields': fields,
                'fieldOrder': card.ord,
                'question': question,
                'answer': answer,
                'buttons': [b[0] for b in reviewer._answerButtonList()],
                'modelName': model['name'],
                'deckName': self.deckNameFromId(card.did),
                'css': model['css'],
                'flagged': card.flags > 0
            }


    def guiStartCardTimer(self):
        if not self.guiReviewActive():
            return False

        card = self.reviewer().card

        if card is not None:
            card.startTimer()
            return True
        else:
            return False

    def guiShowQuestion(self):
        if self.guiReviewActive():
            self.reviewer()._showQuestion()
            return True
        else:
            return False


    def guiShowAnswer(self):
        if self.guiReviewActive():
            self.window().reviewer._showAnswer()
            return True
        else:
            return False


    def guiAnswerCard(self, ease):
        if not self.guiReviewActive():
            return False

        reviewer = self.reviewer()
        if reviewer.state != 'answer':
            return False
        if ease <= 0 or ease > self.scheduler().answerButtons(reviewer.card):
            return False

        reviewer._answerCard(ease)
        return True


    def guiDeckOverview(self, name):
        collection = self.collection()
        if collection is not None:
            deck = collection.decks.byName(name)
            if deck is not None:
                collection.decks.select(deck['id'])
                self.window().onOverview()
                return True

        return False


    def guiDeckBrowser(self):
        self.window().moveToState('deckBrowser')


    def guiDeckReview(self, name):
        if self.guiDeckOverview(name):
            self.window().moveToState('review')
            return True
        else:
            return False

    def guiExitAnki(self):
        timer = QTimer()
        def exitAnki():
            timer.stop()
            self.window().close()
        timer.timeout.connect(exitAnki)
        timer.start(1000) # 1s should be enough to allow the response to be sent.

    def addAudioNote(self, params, audioFile, allowDuplicate=True):
        """
        Add a note with audio file from URL
        
        Args:
            params: Note parameters (deckName, modelName, fields, tags)
            audioFile: URL to MP3 file (e.g., Digital Ocean Spaces URL)
            allowDuplicate: Allow duplicate first field (default: True)
        
        Returns:
            Note ID on success
        """
        try:
            collection = self.collection()
            if collection is None:
                raise Exception("Anki collection not available - is Anki running?")
            
            # Validate model exists
            model_name = params.get('modelName')
            model = collection.models.by_name(model_name)
            if model is None:
                available_models = [n.name for n in collection.models.all_names_and_ids()]
                raise Exception(f"Model '{model_name}' not found. Available models: {', '.join(available_models)}")
            
            # Check if model has Audio1 field
            model_fields = [field['name'] for field in model['flds']]
            if 'Audio1' not in model_fields:
                raise Exception(f"Model '{model_name}' must have an 'Audio1' field. Current fields: {', '.join(model_fields)}")
            
            # Validate deck exists
            deck_name = params.get('deckName')
            deck = collection.decks.by_name(deck_name)
            if deck is None:
                available_decks = [d.name for d in collection.decks.all_names_and_ids()]
                raise Exception(f"Deck '{deck_name}' not found. Available decks: {', '.join(available_decks[:10])}...")
            
            # Validate audioFile
            if not audioFile or not verifyString(audioFile):
                raise Exception("audioFile is required and must be a valid URL string")
            
            # Download audio file from URL
            try:
                audio_data = download(audioFile)
                if audio_data is None:
                    raise Exception(f"Failed to download audio from URL: {audioFile}")
            except Exception as e:
                raise Exception(f"Failed to download audio from URL: {str(e)}")
            
            # Validate that we got audio data
            if not audio_data or len(audio_data) == 0:
                raise Exception("Downloaded file is empty")
            
            # Generate filename from URL
            import urllib.parse
            parsed_url = urllib.parse.urlparse(audioFile)
            url_filename = os.path.basename(parsed_url.path)
            
            if url_filename and '.' in url_filename:
                # Use filename from URL with unique timestamp
                timestamp = int(time())
                name, ext = os.path.splitext(url_filename)
                audioFilename = f"{name}_{timestamp}{ext}"
            else:
                # Generate completely new filename
                timestamp = int(time())
                url_hash = hashlib.md5(audioFile.encode('utf-8')).hexdigest()[:8]
                audioFilename = f"audio_{timestamp}_{url_hash}.mp3"
            
            # Ensure filename has no directory components
            audioFilename = os.path.basename(audioFilename)
            
            # Store audio file in media folder
            try:
                self.media().writeData(audioFilename, audio_data)
            except Exception as e:
                raise Exception(f"Failed to write audio file to media folder: {str(e)}")
            
            # Add audio reference to Audio1 field
            if 'Audio1' not in params['fields']:
                params['fields']['Audio1'] = ''
            params['fields']['Audio1'] += u'[sound:{}]'.format(audioFilename)
            
            # Create the note
            note_params = AnkiNoteParams(params)
            if not note_params.validate():
                raise Exception("Note parameters validation failed. Check deckName, modelName, fields, and tags are all valid strings.")
            
            # Create note object
            model = collection.models.by_name(model_name)
            note = anki.notes.Note(collection, model)
            note.tags = params.get('tags', [])
            
            for name, value in params['fields'].items():
                if name in note:
                    note[name] = value
            
            # Check for duplicates if not allowed
            if not allowDuplicate and note.dupeOrEmpty():
                raise Exception(f"Duplicate note detected. First field already exists.")
            
            # Modern Anki (2.1.50+): collection.add_note() runs in its own Rust
            # transaction and fires the note_will_be_added + op-framework UI
            # hooks itself. Do NOT wrap in startEditing/stopEditing - that calls
            # the obsolete aqt.mw.requireReset(), which prints a stack trace and
            # forces a full UI reset on every call (the addAudioNote slowdown);
            # collection.autosave() is a deprecated no-op. Mirrors addNote().
            try:
                collection.add_note(note, deck['id'])
            except (AttributeError, TypeError):
                # Older Anki: keep the legacy path including the UI-reset wrapper.
                self.startEditing()
                try:
                    collection.addNote(note)
                    cardIds = [card.id for card in note.cards()]
                    if cardIds:
                        self.changeDeck(cardIds, deck_name)
                finally:
                    self.stopEditing()

            return note.id
            
        except Exception as e:
            # Re-raise with context
            raise Exception(f"addAudioNote error: {str(e)}")

    def importPackage(self, packageUrl, parentDeck=None, allowDuplicates=False):
        """
        Import an Anki package (.apkg) from a URL using the modern backend API
        
        Args:
            packageUrl: URL to .apkg file
            parentDeck: Optional parent deck name - cards will be moved to this deck
            allowDuplicates: If True, attempt to update duplicate notes; if False (default), skip duplicates
        
        Returns:
            dict with import success status and log
        """
        tmp_path = None
        
        try:
            collection = self.collection()
            if collection is None:
                raise Exception("Collection not available")
            
            # If parentDeck is specified, ensure it exists
            if parentDeck:
                # Create the parent deck if it doesn't exist
                parent_deck = collection.decks.by_name(parentDeck)
                if parent_deck is None:
                    collection.decks.id(parentDeck)  # This creates the deck
                    parent_deck = collection.decks.by_name(parentDeck)
            
            # Download file
            try:
                if sys.version_info[0] < 3:
                    import urllib2
                    req = urllib2.Request(packageUrl)
                    resp = urllib2.urlopen(req, timeout=60)
                else:
                    from urllib import request
                    req = request.Request(packageUrl)
                    req.add_header('User-Agent', 'AnkiConnect')
                    resp = request.urlopen(req, timeout=60)
                
                if resp.code != 200:
                    raise Exception(f"HTTP {resp.code}: Server returned error")
                
                package_data = resp.read()
                if not package_data or len(package_data) == 0:
                    raise Exception("Downloaded data is empty")
                
                if not package_data.startswith(b'PK\x03\x04'):
                    raise Exception("Downloaded data is not a valid zip/apkg file")
                    
            except Exception as e:
                raise Exception(f"Download failed: {str(e)}")
            
            # Write to temp file
            try:
                with tempfile.NamedTemporaryFile(suffix='.apkg', delete=False) as tmp_file:
                    tmp_file.write(package_data)
                    tmp_file.flush()
                    os.fsync(tmp_file.fileno())
                    tmp_path = tmp_file.name
                
                if not os.path.exists(tmp_path) or os.path.getsize(tmp_path) == 0:
                    raise Exception("Failed to create temporary file")
                    
            except Exception as e:
                if tmp_path and os.path.exists(tmp_path):
                    try:
                        os.unlink(tmp_path)
                    except:
                        pass
                raise Exception(f"Failed to write temporary file: {str(e)}")
            
            # Import using the modern backend API
            try:
                # Import the protobuf classes
                from anki.collection import ImportAnkiPackageRequest, ImportAnkiPackageOptions
                from anki import import_export_pb2
                
                # Get deck names and card IDs before import to track what was imported
                decks_before = {d.name for d in collection.decks.all_names_and_ids()}
                cards_before = set(collection.db.list('select id from cards'))
                
                # Create import options
                options = ImportAnkiPackageOptions()
                options.merge_notetypes = True  # Merge note types if they exist
                options.with_scheduling = True   # Include scheduling information
                options.with_deck_configs = True  # Include deck configurations
                
                # Configure duplicate handling using update_notes field
                if allowDuplicates:
                    # ALWAYS = 1: Always update existing notes if duplicates found
                    options.update_notes = import_export_pb2.ImportAnkiPackageUpdateCondition.IMPORT_ANKI_PACKAGE_UPDATE_CONDITION_ALWAYS
                else:
                    # IF_NEWER = 0: Skip duplicates (default behavior)
                    options.update_notes = import_export_pb2.ImportAnkiPackageUpdateCondition.IMPORT_ANKI_PACKAGE_UPDATE_CONDITION_IF_NEWER
                
                # Create the import request
                request = ImportAnkiPackageRequest()
                request.package_path = tmp_path
                request.options.CopyFrom(options)
                
                # Perform the import using the backend
                result = collection.import_anki_package(request)
                
                # If parentDeck specified, move cards to properly named subdeck
                if parentDeck:
                    # Extract deck name from URL as fallback
                    # URL format: ${userId}_${uuidv4()}_${sanitizedFilename}.apkg
                    extracted_name = None
                    try:
                        import urllib.parse
                        parsed_url = urllib.parse.urlparse(packageUrl)
                        url_filename = os.path.basename(parsed_url.path)
                        
                        # Remove .apkg extension
                        if url_filename.endswith('.apkg'):
                            url_filename = url_filename[:-5]
                        
                        # Split by underscore and skip first two parts (userId and uuid)
                        parts = url_filename.split('_')
                        if len(parts) >= 3:
                            # Join remaining parts (in case sanitized filename has underscores)
                            extracted_name = '_'.join(parts[2:])
                    except:
                        extracted_name = None
                    
                    # Get new cards that were imported
                    cards_after = set(collection.db.list('select id from cards'))
                    new_card_ids = list(cards_after - cards_before)
                    
                    if new_card_ids:
                        # Get new decks that were created by the import
                        decks_after = {d.name for d in collection.decks.all_names_and_ids()}
                        new_decks = decks_after - decks_before
                        
                        # Determine the subdeck name
                        subdeck_name = extracted_name if extracted_name else "imported"
                        
                        # If there are imported decks, check their names
                        if new_decks:
                            first_deck = list(new_decks)[0]
                            # Use extracted name if deck is "Default", otherwise use the deck's name
                            if first_deck.lower() != "default" and not extracted_name:
                                subdeck_name = first_deck
                            elif extracted_name:
                                subdeck_name = extracted_name
                            else:
                                subdeck_name = first_deck
                        
                        # Create the properly named subdeck: parentDeck::subdeck_name
                        target_deck_name = f"{parentDeck}::{subdeck_name}"
                        
                        # Create the deck (this will create parent hierarchy if needed)
                        collection.decks.id(target_deck_name)
                        
                        # Move all imported cards to the target subdeck.
                        # No startEditing()/stopEditing() (requireReset spam);
                        # changeDeck persists directly. GUI refresh gated on toggle.
                        self.changeDeck(new_card_ids, target_deck_name)
                        self.refreshGuiIfEnabled()
                        
                        # Delete the temporary imported decks (cards are already moved)
                        for temp_deck_name in new_decks:
                            try:
                                temp_deck = collection.decks.by_name(temp_deck_name)
                                if temp_deck:
                                    collection.decks.remove([temp_deck['id']])
                            except:
                                pass
                        
                        # Save all changes
                        collection.save()
                
                # Build log message from result
                log_parts = []
                if result.log.new:
                    log_parts.append(f"Added {len(result.log.new)} notes")
                if result.log.updated:
                    log_parts.append(f"Updated {len(result.log.updated)} notes")
                if result.log.duplicate:
                    log_parts.append(f"Skipped {len(result.log.duplicate)} duplicates")
                
                if parentDeck:
                    log_parts.append(f"Imported under '{parentDeck}'")
                
                log = ", ".join(log_parts) if log_parts else "Import completed"
                
                return {
                    'success': True,
                    'log': log,
                    'notes_added': len(result.log.new),
                    'notes_updated': len(result.log.updated),
                    'notes_duplicate': len(result.log.duplicate)
                }
                
            except Exception as e:
                # If backend import fails, provide helpful error
                raise Exception(f"Import failed: {str(e)}")
                    
        except Exception as e:
            error_msg = str(e)
            if "Invalid file" in error_msg:
                raise Exception("Invalid .apkg file. The file may be corrupted.")
            else:
                raise Exception(f"Import error: {error_msg}")
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except:
                    pass



#
# AnkiConnect
#

class AnkiConnect:
    def __init__(self):
        self.anki = AnkiBridge()
        self.server = AjaxServer(self.handler)

        try:
            self.server.listen()

            self.timer = QTimer()
            self.timer.timeout.connect(self.advance)
            self.timer.start(TICK_INTERVAL)
        except:
            QMessageBox.critical(
                self.anki.window(),
                'AnkiConnect',
                'Failed to listen on port {}.\nMake sure it is available and is not in use.'.format(NET_PORT)
            )

        # Persisted settings store + Tools-menu toggle. Loads the
        # "refresh GUI after API writes" flag onto the bridge (default off).
        self._config = ConfigManager()
        self._setupSettingsMenu()
        self._stopDeckListRefreshTimer()


    def _setupSettingsMenu(self):
        """Add a persisted, checkable Tools-menu item: 'Global Edge: Refresh
        GUI after API writes'. Off by default so the headless server does no
        UI work per request; turn it on when watching the Anki desktop GUI for
        debugging, and raw-write endpoints (e.g. undoAnswerCard) repaint the
        reviewer/deck list via the modern mw.reset(). The setting persists
        across restarts AND add-on updates (stored in Anki's base folder; see
        utils/config_manager.py). Mirrors the earlier ElevenLabs settings menu."""
        try:
            import aqt
            from aqt.qt import QAction

            enabled = bool(self._config.get(CONFIG_GUI_REFRESH, False))
            self.anki.guiRefreshAfterApiWrites = enabled

            action = QAction('Global Edge: Refresh GUI after API writes', aqt.mw)
            action.setCheckable(True)
            action.setChecked(enabled)
            action.toggled.connect(self._onToggleGuiRefresh)
            aqt.mw.form.menuTools.addSeparator()
            aqt.mw.form.menuTools.addAction(action)
            self._guiRefreshAction = action  # retain ref so Qt doesn't GC it
        except Exception:
            pass  # never let menu setup break server startup


    def _onToggleGuiRefresh(self, checked):
        """Persist the toggle and update the live flag the endpoints read."""
        enabled = bool(checked)
        self.anki.guiRefreshAfterApiWrites = enabled
        self._config.set(CONFIG_GUI_REFRESH, enabled)


    def _stopDeckListRefreshTimer(self):
        """Stop Anki's 10-minute deck-list auto-refresh (aqt main.py
        setup_timers -> onRefreshTimer). Each tick re-renders the whole deck
        list - every student's decks in one page - and Anki 26.09's QtWebEngine
        doesn't free the old page: the web view grew ~10-15 MB per tick until
        the 600M cgroup cap OOM-killed it every ~5 h. Nobody watches the server
        GUI; click Decks to refresh it by hand. Anki creates its timers in
        setupUI() before add-ons load, so the timer exists by now, and it's the
        main window's only repeating 10-minute QTimer."""
        try:
            import aqt
            from aqt.qt import QTimer

            timers = [
                t for t in aqt.mw.findChildren(QTimer)
                if t.interval() == 10 * 60 * 1000 and not t.isSingleShot()
            ]
            for t in timers:
                t.stop()
            if len(timers) != 1:
                # Anki changed its timers; the leak returns until this matches again.
                print('Global Edge AnkiConnect: expected 1 deck-list refresh timer, stopped {}'.format(len(timers)))
        except Exception:
            pass  # never let this break server startup


    def advance(self):
        self.server.advance()


    def handler(self, request):
        name = request.get('action', '')
        version = request.get('version', 4)
        params = request.get('params', {})
        reply = {'result': None, 'error': None}

        try:
            method = None

            for methodName, methodInst in inspect.getmembers(self, predicate=inspect.ismethod):
                apiVersionLast = 0
                apiNameLast = None

                if getattr(methodInst, 'api', False):
                    for apiVersion, apiName in getattr(methodInst, 'versions', []):
                        if apiVersionLast < apiVersion <= version:
                            apiVersionLast = apiVersion
                            apiNameLast = apiName

                    if apiNameLast is None and apiVersionLast == 0:
                        apiNameLast = methodName

                    if apiNameLast is not None and apiNameLast == name:
                        method = methodInst
                        break

            if method is None:
                raise Exception('unsupported action')
            else:
                reply['result'] = methodInst(**params)
        except Exception as e:
            reply['error'] = str(e)

        if version > 4:
            return reply
        else:
            return reply['result']


    @webApi()
    def multi(self, actions):
        return self.anki.multi(actions)


    @webApi()
    def storeMediaFile(self, filename, data):
        return self.anki.storeMediaFile(filename, data)


    @webApi()
    def retrieveMediaFile(self, filename):
        return self.anki.retrieveMediaFile(filename)


    @webApi()
    def deleteMediaFile(self, filename):
        return self.anki.deleteMediaFile(filename)


    @webApi()
    def deckNames(self):
        return self.anki.deckNames()


    @webApi()
    def deckNamesAndIds(self):
        return self.anki.deckNamesAndIds()


    @webApi()
    def modelNames(self):
        return self.anki.modelNames()


    @webApi()
    def modelNamesAndIds(self):
        return self.anki.modelNamesAndIds()


    @webApi()
    def modelFieldNames(self, modelName):
        return self.anki.modelFieldNames(modelName)


    @webApi()
    def modelFieldsOnTemplates(self, modelName):
        return self.anki.modelFieldsOnTemplates(modelName)


    @webApi()
    def getDeckConfig(self, deck):
        return self.anki.getDeckConfig(deck)


    @webApi()
    def saveDeckConfig(self, config):
        return self.anki.saveDeckConfig(config)


    @webApi()
    def setDeckConfigId(self, decks, configId):
        return self.anki.setDeckConfigId(decks, configId)


    @webApi()
    def cloneDeckConfigId(self, name, cloneFrom=1):
        return self.anki.cloneDeckConfigId(name, cloneFrom)


    @webApi()
    def removeDeckConfigId(self, configId):
        return self.anki.removeDeckConfigId(configId)


    @webApi()
    def addNote(self, note):
        params = AnkiNoteParams(note)
        if not params.validate():
            raise Exception("Note parameters validation failed")
        
        result = self.anki.addNote(params)
        if result is None:
            raise Exception("Failed to create note - no note ID returned")
        return result


    @webApi()
    def addNotes(self, notes):
        results = []
        for note in notes:
            params = AnkiNoteParams(note)
            if params.validate():
                results.append(self.anki.addNote(params))
            else:
                results.append(None)

        return results

    @webApi()
    def updateNoteFields(self, note):
        """
        Update note fields with optional audio download and deck validation (unified API)
        
        Args:
            note (dict): {
                'id': noteId (required),
                'deckName': parent deck name (required) - validates note belongs to this deck,
                'fields': {fieldName: value, ...} (optional - text fields),
                'audioFields': {fieldName: audioUrl, ...} (optional - audio from URLs)
            }
            
        Returns:
            dict: Updated note information including noteId, fields, tags, modelName, and card IDs
            
        Examples:
            # Update text fields only
            updateNoteFields({
                'id': 1234567890,
                'deckName': 'local_67769a9c-6770-44ec-8c88-81190f6e78bd',
                'fields': {'Front': 'New text', 'Back': 'Updated'}
            })
            
            # Update audio field only
            updateNoteFields({
                'id': 1234567890,
                'deckName': 'MyDeck',
                'audioFields': {'Audio': 'https://example.com/audio.mp3'}
            })
            
            # Update both text and audio
            updateNoteFields({
                'id': 1234567890,
                'deckName': 'MyDeck',
                'fields': {'Front': 'New text'},
                'audioFields': {'Audio': 'https://example.com/audio.mp3'}
            })
            
        Note:
            - deckName is REQUIRED - validates at least one card belongs to deck or subdeck
            - Card timing data (intervals, ease, due dates) is automatically preserved
            - Audio replaces the entire field content with [sound:filename.mp3]
            - All cards from the same note are updated with new field values
            - Creates an undo entry in Anki
            - Case-sensitive deck matching
        """
        return self.anki.updateNoteFields(note)

    @webApi()
    def canAddNotes(self, notes):
        results = []
        for note in notes:
            params = AnkiNoteParams(note)
            results.append(params.validate() and self.anki.canAddNote(params))

        return results


    @webApi()
    def addTags(self, notes, tags, add=True):
        return self.anki.addTags(notes, tags, add)


    @webApi()
    def removeTags(self, notes, tags):
        return self.anki.addTags(notes, tags, False)


    @webApi()
    def getTags(self):
        return self.anki.getTags()


    @webApi()
    def suspend(self, cards, suspend=True):
        return self.anki.suspend(cards, suspend)


    @webApi()
    def unsuspend(self, cards):
        return self.anki.suspend(cards, False)


    @webApi()
    def areSuspended(self, cards):
        return self.anki.areSuspended(cards)


    @webApi()
    def areDue(self, cards):
        return self.anki.areDue(cards)


    @webApi()
    def getIntervals(self, cards, complete=False):
        return self.anki.getIntervals(cards, complete)


    @webApi()
    def upgrade(self):
        response = QMessageBox.question(
            self.anki.window(),
            'AnkiConnect',
            'Upgrade to the latest version?',
            QMessageBox.Yes | QMessageBox.No
        )

        if response == QMessageBox.Yes:
            data = download(URL_UPGRADE)
            if data is None:
                QMessageBox.critical(self.anki.window(), 'AnkiConnect', 'Failed to download latest version.')
            else:
                path = os.path.splitext(__file__)[0] + '.py'
                with open(path, 'w') as fp:
                    fp.write(makeStr(data))
                QMessageBox.information(self.anki.window(), 'AnkiConnect', 'Upgraded to the latest version, please restart Anki.')
                return True

        return False


    @webApi()
    def version(self):
        return API_VERSION
    
    @webApi()
    def addonVersion(self):
        """Get the add-on version"""
        return ADDON_VERSION


    @webApi()
    def gcStats(self, collect=False, topTypes=0, saveall=False):
        """Diagnostic: Python GC / object-count snapshot for leak probing.

        Anki runs gc.disable() at startup, so a per-request reference cycle is
        never collected and leaks forever. Process RSS is too noisy to see a
        tens-of-KB/call cycle (allocator arena churn swamps it); the count of
        live Python objects is not. This lets a probe measure
        objects-leaked-per-call instead of fighting RSS noise.

        Args:
            collect (bool): if True, run gc.collect() and report how many
                cycle objects it reclaimed (the count that would have leaked
                under Anki's disabled GC) plus the object count afterwards. A
                probe uses this to reset to a clean baseline between endpoints;
                it mutates process state, so don't call it on a live instance
                you're trying to passively observe.
            topTypes (int): if > 0, also return the N most common object types
                by count (of the WHOLE heap — steady state plus any uncollected
                leak; for the leak's true composition use saveall instead).
            saveall (bool): with collect=True, histogram EXACTLY the objects
                gc.collect() reclaims (the per-call cycle leak) via
                DEBUG_SAVEALL, returned as 'garbageTypes'. Frees them after.

        Returns:
            dict with 'objects' (len(gc.get_objects())), 'garbage'
            (len(gc.garbage)), 'enabled' (gc.isenabled(), expected False under
            Anki), and — when requested — 'collected', 'objectsAfter',
            'topTypes' ([[typeName, count], ...]).
        """
        import gc
        objs = gc.get_objects()
        result = {
            'objects': len(objs),
            'garbage': len(gc.garbage),
            'enabled': gc.isenabled(),
        }
        if topTypes and int(topTypes) > 0:
            from collections import Counter
            counter = Counter(type(o).__name__ for o in objs)
            result['topTypes'] = [list(pair) for pair in counter.most_common(int(topTypes))]
            del counter
        del objs  # don't retain the all-objects list past this point
        if collect and saveall:
            # Histogram EXACTLY the objects this collect reclaims. DEBUG_SAVEALL
            # diverts collected cycle objects into gc.garbage instead of freeing
            # them, so we can read the leak's true composition (the absolute heap
            # snapshot above can't isolate it). We then drop our refs and collect
            # again with the flag off, so the diagnostic itself leaks nothing.
            from collections import Counter
            prev = len(gc.garbage)
            old_flags = gc.get_debug()
            gc.set_debug(gc.DEBUG_SAVEALL)
            try:
                result['collected'] = gc.collect()
                leaked = gc.garbage[prev:]
                n = int(topTypes) if (topTypes and int(topTypes) > 0) else 20
                hist = Counter(type(o).__name__ for o in leaked)
                result['garbageTypes'] = [list(pair) for pair in hist.most_common(n)]
                del leaked, hist
            finally:
                del gc.garbage[prev:]      # release our refs to the saved objects
                gc.set_debug(old_flags)    # restore (DEBUG_SAVEALL off)
                gc.collect()               # now actually free those cycles
            result['objectsAfter'] = len(gc.get_objects())
        elif collect:
            result['collected'] = gc.collect()
            result['objectsAfter'] = len(gc.get_objects())
        return result
    
    @webApi()
    def debugInfo(self):
        """Debug endpoint to check what methods are available"""
        import sys
        info = {
            'apiVersion': API_VERSION,
            'ankiConnectFile': __file__,
            'studyManagerFile': None,
            'availableMethods': [],
            'answerCardSignature': None
        }
        
        # Get study_manager file location
        try:
            import study_manager
            info['studyManagerFile'] = study_manager.__file__
        except:
            pass
        
        # Check answerCard method signature
        try:
            import inspect as insp
            sig = insp.signature(self.answerCard)
            info['answerCardSignature'] = str(sig)
            info['answerCardParameters'] = list(sig.parameters.keys())
        except Exception as e:
            info['answerCardSignature'] = f"Error: {str(e)}"
        
        # List all @webApi methods
        for methodName, methodInst in inspect.getmembers(self, predicate=inspect.ismethod):
            if getattr(methodInst, 'api', False):
                info['availableMethods'].append(methodName)
        
        return info


    @webApi()
    def findNotes(self, query=None):
        return self.anki.findNotes(query)


    @webApi()
    def findCards(self, query=None):
        return self.anki.findCards(query)


    @webApi()
    def getDecks(self, cards):
        return self.anki.getDecks(cards)


    @webApi()
    def changeDeck(self, cards, deck):
        return self.anki.changeDeck(cards, deck)


    @webApi()
    def deleteDecks(self, decks, cardsToo=False):
        return self.anki.deleteDecks(decks, cardsToo)


    @webApi()
    def cardsToNotes(self, cards):
        return self.anki.cardsToNotes(cards)


    @webApi()
    def guiBrowse(self, query=None):
        return self.anki.guiBrowse(query)


    @webApi()
    def guiAddCards(self):
        return self.anki.guiAddCards()


    @webApi()
    def guiCurrentCard(self):
        return self.anki.guiCurrentCard()


    @webApi()
    def guiStartCardTimer(self):
        return self.anki.guiStartCardTimer()


    @webApi()
    def guiAnswerCard(self, ease):
        return self.anki.guiAnswerCard(ease)


    @webApi()
    def guiShowQuestion(self):
        return self.anki.guiShowQuestion()


    @webApi()
    def guiShowAnswer(self):
        return self.anki.guiShowAnswer()


    @webApi()
    def guiDeckOverview(self, name):
        return self.anki.guiDeckOverview(name)


    @webApi()
    def guiDeckBrowser(self):
        return self.anki.guiDeckBrowser()


    @webApi()
    def guiDeckReview(self, name):
        return self.anki.guiDeckReview(name)


    @webApi()
    def guiExitAnki(self):
        return self.anki.guiExitAnki()

    @webApi()
    def addAudioNote(self, note, audioFile, allowDuplicate=True):
        """
        Add a note with audio file from URL
        
        Args:
            note: Note parameters (deckName, modelName, fields, tags)
            audioFile: URL to MP3 file (e.g., Digital Ocean Spaces URL)
            allowDuplicate: Allow duplicate notes (default: True)
        
        Returns:
            Note ID on success
        """
        return self.anki.addAudioNote(note, audioFile, allowDuplicate)

    @webApi()
    def importPackage(self, packageUrl, parentDeck=None, allowDuplicates=False):
        """
        Import an Anki package (.apkg) from a URL
        
        Args:
            packageUrl: URL to .apkg file (e.g., Digital Ocean Spaces URL)
            parentDeck: Optional parent deck name to import under (preserves original deck structure)
            allowDuplicates: If True, import duplicate notes; if False (default), skip duplicates
        
        Returns:
            dict with import statistics including success status, notes added/updated/duplicated, and deck name
        """
        return self.anki.importPackage(packageUrl, parentDeck, allowDuplicates)

    @webApi()
    def cardsInfo(self, cards):
        return self.anki.cardsInfo(cards)

    @webApi()
    def notesInfo(self, notes):
        return self.anki.notesInfo(notes)

    # Note/Model Management - Simple current profile usage
    @webApi()
    def createModel(self, modelName, fields, templates, css=""):
        return NoteManager(self.anki).createModel(modelName, fields, templates, css)

    @webApi()
    def updateModel(self, modelId, modelName=None, fields=None, templates=None, css=None):
        return NoteManager(self.anki).updateModel(modelId, modelName, fields, templates, css)

    @webApi()
    def deleteModel(self, modelId):
        return NoteManager(self.anki).deleteModel(modelId)

    @webApi()
    def createDeck(self, deckName):
        return NoteManager(self.anki).createDeck(deckName)

    @webApi()
    def getModelInfo(self, modelId):
        return NoteManager(self.anki).getModelInfo(modelId)

    @webApi()
    def getDeckInfo(self, deckName, includeTimeStats=True, period="allTime", wantSingleDeckStats=False):
        return NoteManager(self.anki).getDeckInfo(deckName, includeTimeStats, period, wantSingleDeckStats)

    @webApi()
    def deleteDeck(self, deckName, deleteCards=False):
        return NoteManager(self.anki).deleteDeck(deckName, deleteCards)

    @webApi()
    def resetDecks(self, ownerDeck, deckNames, keepDeckNames=None):
        return NoteManager(self.anki).resetDecks(ownerDeck, deckNames, keepDeckNames)

    @webApi()
    def renameDeck(self, oldName, newName):
        return NoteManager(self.anki).renameDeck(oldName, newName)

    # Study Management - Direct usage  
    @webApi()
    def getNextReviewCard(self, deckName=None, needRender=False):
        return StudyManager(self.anki).getNextReviewCard(deckName, needRender)

    @webApi()
    def answerCard(self, cardId, ease, timeTakenSeconds=None, deckName=None):
        return StudyManager(self.anki).answerCard(cardId, ease, timeTakenSeconds, deckName)

    @webApi()
    def resetCard(self, cardId):
        return StudyManager(self.anki).resetCard(cardId)

    @webApi()
    def forgetCard(self, cardId):
        return StudyManager(self.anki).forgetCard(cardId)

    @webApi()
    def getDueCards(self, deckName=None, limit=10):
        return StudyManager(self.anki).getDueCards(deckName, limit)

    @webApi()
    def getNewCards(self, deckName=None, limit=10):
        return StudyManager(self.anki).getNewCards(deckName, limit)

    @webApi()
    def getStudyStats(self, deckName=None):
        return StudyManager(self.anki).getStudyStats(deckName)

    @webApi()
    def getDeckTimeStats(self, deckName=None, period="allTime"):
        return StudyManager(self.anki).getDeckTimeStats(deckName, period)

    @webApi()
    def flagCard(self, cardId, deckName=None):
        """
        Flag a card with red flag
        
        Args:
            cardId (int): ID of the card to flag
            deckName (str, optional): Reject a card outside this deck and its subdecks
            
        Returns:
            bool: True if successful
        """
        return self.anki.flagCard(cardId, deckName)

    @webApi()
    def unflagCard(self, cardId, deckName=None):
        """
        Remove flag from a card
        
        Args:
            cardId (int): ID of the card to unflag
            deckName (str, optional): Reject a card outside this deck and its subdecks
            
        Returns:
            bool: True if successful
        """
        return self.anki.unflagCard(cardId, deckName)

    @webApi()
    def isCardFlagged(self, cardId):
        """
        Check if a card is flagged
        
        Args:
            cardId (int): ID of the card to check
            
        Returns:
            bool: True if card is flagged
        """
        return self.anki.isCardFlagged(cardId)

    @webApi()
    def setDeckStudyOptions(self, deckName, newCardsPerDay=None, reviewsPerDay=None):
        """
        Set study options for a deck (convenience wrapper for getDeckConfig/saveDeckConfig)
        
        Args:
            deckName (str): Name of the deck to configure
            newCardsPerDay (int, optional): Maximum new cards to study per day
            reviewsPerDay (int, optional): Maximum review cards per day
            
        Returns:
            dict: Updated configuration with the new settings
        """
        return StudyManager(self.anki).setDeckStudyOptions(deckName, newCardsPerDay, reviewsPerDay)

    @webApi()
    def extendNewCardLimit(self, deckName, additionalCards):
        """
        Extend today's new card limit for a specific deck
        
        Args:
            deckName (str): Name of the deck
            additionalCards (int): Number of additional new cards to allow today
            
        Returns:
            dict: Information about the extended limit
        """
        return StudyManager(self.anki).extendNewCardLimit(deckName, additionalCards)

    @webApi()
    def enableStudyForgotten(self, deckName, days=1, filteredDeckName=None):
        """
        Create a filtered deck for studying cards that were forgotten (answered "Again") in the last X days.
        Matches Anki's native "Review forgotten cards" custom study option.
        
        Args:
            deckName (str): Name of the source deck
            days (int): Look back this many days for forgotten cards (default: 1 = today only)
            filteredDeckName (str, optional): Custom name for the filtered deck. If not provided, auto-generates name.
            
        Returns:
            dict: Information about the created filtered deck
        """
        return StudyManager(self.anki).enableStudyForgotten(deckName, days, filteredDeckName)

    @webApi()
    def createCustomStudy(self, deckName, newCardsPerDay=None, reviewsPerDay=None, 
                         studyForgottenToday=False, extendNewLimit=None):
        """
        Combined API for creating a custom study session with various options
        
        Args:
            deckName (str): Name of the deck to configure
            newCardsPerDay (int, optional): Set new cards per day limit
            reviewsPerDay (int, optional): Set reviews per day limit
            studyForgottenToday (bool): Create filtered deck for forgotten cards today
            extendNewLimit (int, optional): Extend today's new card limit by this many cards
            
        Returns:
            dict: Results of all requested operations
        """
        return StudyManager(self.anki).createCustomStudy(
            deckName, newCardsPerDay, reviewsPerDay, studyForgottenToday, extendNewLimit
        )
    
    @webApi()
    def getNoteIds(self, deckName=None, page=1, pageSize=50, query=None):
        """
        Get a paginated list of note IDs using an optional deck name and/or search query.

        Supports the same search syntax as Anki's card browser (e.g. "flag:1", "is:due",
        "tag:mytag", "front:hello", "added:7", etc.).

        At least one of 'deckName' or 'query' must be provided.

        Args:
            deckName (str, optional): Name of the parent deck (subdecks automatically included)
            page (int): 1-indexed page number (default: 1)
            pageSize (int): Number of note IDs per page (default: 50)
            query (str, optional): Anki search query string — same syntax as the card browser.
                                   If both deckName and query are given, they are ANDed together.

        Returns:
            dict: {
                'noteIds': list of note IDs for the requested page,
                'page': current page number,
                'pageSize': page size used,
                'total': total number of matching notes,
                'totalPages': total number of pages,
                'query': the final query string that was executed
            }
        """
        return NoteManager(self.anki).getNoteIds(deckName, page, pageSize, query)

    @webApi()
    def deleteNote(self, noteId, deckName):
        """
        Delete a note and all its cards by note ID.

        Args:
            noteId (int): ID of the note to delete
            deckName (str): Parent deck name. At least one card of the note must
                            belong to this deck or one of its subdecks — the
                            request is rejected otherwise.

        Returns:
            bool: True if successful
        """
        return NoteManager(self.anki).deleteNote(noteId, deckName)

    @webApi()
    def undoAnswerCard(self, cardId, deckName):
        """
        Undo the most recent answer for a specific card.

        Deletes the most recent revlog entry for the card and restores the card's
        scheduling state to what it was before that answer. The card will be
        immediately available in the review queue after undo.

        Args:
            cardId (int): ID of the card whose most recent answer should be undone
            deckName (str): Parent deck name. The card must belong to this deck or
                            one of its subdecks — the request is rejected otherwise.

        Returns:
            dict: {
                'cardId': int,
                'restoredState': str,    # 'new', 'learning', or 'review'
                'restoredInterval': int  # interval the card had before the answer
            }
        """
        return StudyManager(self.anki).undoAnswerCard(cardId, deckName)

    @webApi()
    def getDeckReviewsByDay(self, deckName, days=14):
        """
        Get the number of reviews completed per day for the last N days.
        Uses the same approach as Anki's built-in statistics calendar.

        Args:
            deckName (str): Name of the deck
            days (int): Number of days to look back (default: 14)

        Returns:
            dict: Daily review statistics with breakdown by card type (learning, review, relearn, filtered)
        """
        return StudyManager(self.anki).getDeckReviewsByDay(deckName, days)

    @webApi()
    def getDeckReviewsByDayMulti(self, deckNames, days=14):
        """
        Batched version of getDeckReviewsByDay for many decks. Replaces
        N×(2 queries) with 2 SQLite queries total — intended for callers
        that previously fanned out via callAnkiMulti.

        Args:
            deckNames (list[str]): Deck names to fetch stats for.
            days (int): Number of days to look back (default: 14).

        Returns:
            list[dict]: Per-deck results in the same order as deckNames,
            each with the same shape as getDeckReviewsByDay. Missing
            decks return empty stats rather than raising.
        """
        return StudyManager(self.anki).getDeckReviewsByDayMulti(deckNames, days)

#
#   Entry
#

ac = AnkiConnect()
