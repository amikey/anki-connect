#!/usr/bin/env python3
"""
Check that answerCard, flagCard and unflagCard refuse a card outside the given deckName.

Needs Anki running with this add-on on localhost:8765 and at least two top-level decks,
the first holding a card. It answers nothing and leaves every flag as it found it.
"""

import requests

URL = 'http://localhost:8765'


def call(action, **params):
    return requests.post(URL, json={'action': action, 'version': 6, 'params': params}).json()


def main():
    top_level = [name for name in call('deckNames')['result'] if '::' not in name]
    if len(top_level) < 2:
        print('Need at least two top-level decks')
        return
    own_deck, other_deck = top_level[0], top_level[1]

    cards = call('getDueCards', deckName=own_deck, limit=1).get('result') or call('getNewCards', deckName=own_deck, limit=1).get('result')
    if not cards:
        print(f"No card found in '{own_deck}'")
        return
    card_id = cards[0]['cardId'] if isinstance(cards[0], dict) else cards[0]
    print(f"Card {card_id} from '{own_deck}', checked against '{other_deck}'")

    failures = 0
    for action, extra in [('answerCard', {'ease': 3}), ('flagCard', {}), ('unflagCard', {})]:
        error = call(action, cardId=card_id, deckName=other_deck, **extra).get('error') or ''
        ok = 'does not belong to deck' in error
        failures += not ok
        print(f"  {action} with another deck: {'refused' if ok else 'NOT refused: ' + repr(error)}")

    was_flagged = call('isCardFlagged', cardId=card_id).get('result')
    for action in ['flagCard', 'unflagCard']:
        result = call(action, cardId=card_id, deckName=own_deck)
        ok = result.get('result') is True
        failures += not ok
        print(f"  {action} with its own deck: {'ok' if ok else 'FAILED: ' + repr(result.get('error'))}")
    if was_flagged:
        call('flagCard', cardId=card_id)

    print('PASS' if failures == 0 else f'FAIL ({failures})')


if __name__ == '__main__':
    main()
