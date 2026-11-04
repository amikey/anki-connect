# AnkiConnect Tests

This directory contains tests for the AnkiConnect plugin.

## Test Files

### Unit Tests (tests/)

- `test_decks.py` - Tests for deck-related API endpoints
- `test_misc.py` - Tests for miscellaneous API functionality
- `test_answer_card.py` - Tests for the answerCard API with timeTakenSeconds parameter
- `test_deck_ownership.py` - answerCard, flagCard and unflagCard refuse a card outside the given deckName

### Utility Files

- `util.py` - Helper functions for making API calls to AnkiConnect
- `docker/` - Docker configurations for different Anki versions

## Running Tests

### Prerequisites

1. **Anki must be running** with AnkiConnect installed
2. AnkiConnect should be listening on `http://localhost:8765` (default)
3. Python 3.x with `requests` library installed

### Install Dependencies

```bash
pip install requests
```

### Running Individual Tests

#### Test the answerCard API

```bash
cd tests
python test_answer_card.py
```

This test verifies:

- ✓ AnkiConnect version
- ✓ Deck listing
- ✓ Getting review cards
- ✓ Answering cards WITHOUT timeTakenSeconds (backward compatibility)
- ✓ Answering cards WITH timeTakenSeconds (new feature)
- ✓ Time statistics tracking

#### Run Unit Tests

```bash
cd tests
python -m unittest test_decks.py
python -m unittest test_misc.py
```

Or run all tests:

```bash
cd tests
python -m unittest discover
```

### Running Tests with Docker

The `docker/` folder contains Docker configurations for testing with different Anki versions:

- `2.0.x/` - Configuration for Anki 2.0.x
- `2.1.x/` - Configuration for Anki 2.1.x

See individual Docker folders for instructions on running tests in isolated environments.

## Test Output

### Successful Test Example

```
Testing AnkiConnect API...
--------------------------------------------------

1. Checking AnkiConnect version...
   Response: {'result': 6, 'error': None}

2. Getting deck names...
   Found 3 decks: ['Default', 'English', 'Math']...

3. Getting next review card from 'Default'...
   Found card ID: 1234567890

4. Testing answerCard WITHOUT timeTakenSeconds (backward compatibility)...
   ✓ Success: True

5. Getting another card for time tracking test...
   Found card ID: 1234567891

6. Testing answerCard WITH timeTakenSeconds=15.5...
   ✓ Success: True

7. Verifying time was recorded...
   Total reviews today: 2
   Total time: 15.5 seconds
   Average time per card: 7.75 seconds
```

## Troubleshooting

### Connection Error

```
ERROR: Cannot connect to AnkiConnect at http://localhost:8765
Make sure Anki is running with AnkiConnect installed.
```

**Solution:** Start Anki and ensure AnkiConnect is properly installed.

### No Cards Available

```
No cards due for review
No due cards found. Cannot test answerCard.
```

**Solution:** This is normal if you don't have any cards due. The test will skip card-related tests.

### timeTakenSeconds Not Recognized

```
❌ Error: The timeTakenSeconds parameter is not recognized!
```

**Solution:**

1. Restart Anki after updating the plugin
2. Clear Python bytecode cache: `find . -name "*.pyc" -delete`
3. Verify the updated files are in the correct location

## Contributing

When adding new tests:

1. Use the `callAnkiConnectEndpoint` helper from `util.py`
2. Follow the existing test structure
3. Document expected behavior and error cases
4. Test both success and failure scenarios
