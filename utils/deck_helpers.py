# Copyright (C) 2025
# Deck Helper Functions for AnkiConnect
#
# This module provides utility functions for deck operations,
# particularly for managing deck hierarchies and configurations.

import anki.utils


def get_direct_child_decks(collection, parent_name):
    """
    Get list of direct child decks for a parent deck.
    Only returns immediate children, not grandchildren.
    
    Args:
        collection: Anki collection object
        parent_name (str): Name of the parent deck
        
    Returns:
        list: List of direct child deck names
    """
    if collection is None:
        return []

    # decks.children() is a cached backend RPC returning (name, id) for every
    # descendant. Beats scanning collection.decks.decks, which materialises
    # every deck dict on each call.
    parent_id = collection.decks.id_for_name(parent_name)
    if parent_id is None:
        return []

    children = []
    prefix = parent_name + '::'

    for deck_name, _did in collection.decks.children(parent_id):
        # Only include if there are no more '::' (direct child only)
        if '::' not in deck_name[len(prefix):]:
            children.append(deck_name)

    return children


def get_deck_limits(collection, deck_name):
    """
    Get the current newCardsPerDay and reviewsPerDay limits for a deck.
    
    Args:
        collection: Anki collection object
        deck_name (str): Name of the deck
        
    Returns:
        tuple: (newCardsPerDay, reviewsPerDay)
    """
    if collection is None:
        return (0, 0)
    
    deck = collection.decks.by_name(deck_name)
    if deck is None:
        return (0, 0)
    
    # Get the deck's configuration
    config = collection.decks.config_dict_for_deck_id(deck['id'])
    if config is None:
        return (0, 0)
    
    new_cards = config.get('new', {}).get('perDay', 0)
    reviews = config.get('rev', {}).get('perDay', 0)
    
    return (new_cards, reviews)


def update_parent_deck_silent(collection, parent_name, new_cards_total, reviews_total):
    """
    Silently update a parent deck's study limits without returning info.
    
    Args:
        collection: Anki collection object
        parent_name (str): Name of the parent deck
        new_cards_total (int): Total new cards per day for all children
        reviews_total (int): Total reviews per day for all children
    """
    if collection is None:
        return
    
    # Get the parent deck
    parent_deck = collection.decks.by_name(parent_name)
    if parent_deck is None:
        return
    
    parent_id = parent_deck['id']
    current_config_id = parent_deck.get('conf', 1)
    
    try:
        # Check if this config is shared by other decks. decks_using_config()
        # is Anki's own helper; it returns int DeckIds, so compare against the
        # int parent_id (the old decks.decks scan yielded string keys).
        current_config = collection.decks.get_config(current_config_id)
        is_shared = current_config is not None and any(
            did != parent_id for did in collection.decks.decks_using_config(current_config)
        )

        # If config is shared, clone it for this deck only
        if is_shared:
            new_config_name = f"{parent_name} Options"
            new_config_id = collection.decks.add_config_returning_id(new_config_name, current_config)
            
            # Assign the new config to this deck
            parent_deck['conf'] = new_config_id
            collection.decks.save(parent_deck)
            
            # Get the newly created config
            config = collection.decks.get_config(new_config_id)
        else:
            # Config is not shared, safe to modify directly
            config = collection.decks.config_dict_for_deck_id(parent_id)
        
        if config is None:
            return
        
        # Update the limits
        config['new']['perDay'] = new_cards_total
        config['rev']['perDay'] = reviews_total
        
        # Save the configuration
        config['mod'] = anki.utils.intTime()
        config['usn'] = collection.usn()
        collection.decks.save(config)
        collection.autosave()
        
    except Exception:
        # Silently fail - don't interrupt the main operation
        pass


def assert_card_in_deck(collection, card_id, card_did, deck_name):
    """
    Raise unless the card is in deck_name or one of its subdecks.

    Card ids are global across every student's decks, so the deckName the API
    builds from its caller is the only ownership check.
    """
    parent_deck = collection.decks.by_name(deck_name)
    if parent_deck is None:
        raise Exception(f"Deck '{deck_name}' does not exist")

    card_deck_name = collection.decks.get(card_did)['name']
    if card_deck_name != deck_name and not card_deck_name.startswith(deck_name + '::'):
        raise Exception(
            f"Card '{card_id}' does not belong to deck '{deck_name}' or its subdecks "
            f"(card is in '{card_deck_name}')"
        )
