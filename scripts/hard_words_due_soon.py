#!/usr/bin/env python3
"""
Find well-learned flashcards that are due soon.

Queries a running Anki instance (via the AnkiConnect add-on) for cards in a
given deck that have been reviewed at least `--min-reps` times and are due
within the next `--days` days. Prints a summary grouped by due day, and
optionally writes the full list to a CSV file.

Requires Anki to be running with AnkiConnect installed and listening on
http://localhost:8765 (the default).
"""

import argparse
import csv
import json
import re
import sys
import urllib.request

ANKICONNECT_URL = "http://localhost:8765"


def invoke(action, **params):
    payload = json.dumps({"action": action, "version": 6, "params": params}).encode("utf-8")
    req = urllib.request.Request(ANKICONNECT_URL, payload)
    try:
        response = json.load(urllib.request.urlopen(req))
    except Exception as e:
        sys.exit(f"Could not reach AnkiConnect at {ANKICONNECT_URL} — is Anki running? ({e})")
    if response.get("error") is not None:
        sys.exit(f"AnkiConnect error: {response['error']}")
    return response["result"]


def strip_html(value):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", value)).strip()


def front_text(card):
    fields = card["fields"]
    if "Front" in fields:
        return strip_html(fields["Front"]["value"])
    first_field = next(iter(fields.values()))
    return strip_html(first_field["value"])


def find_due_soon(deck, min_reps, days):
    """Return {day_offset: [cardId, ...]} for day_offset in 0..days, deduped
    so each card appears only in the bucket for its soonest due day."""
    base_query = f'deck:"{deck}" prop:reps>={min_reps} -is:suspended'
    seen = set()
    buckets = {}
    for offset in range(days + 1):
        ids = set(invoke("findCards", query=f"{base_query} prop:due<={offset}"))
        buckets[offset] = sorted(ids - seen)
        seen |= ids
    return buckets


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deck", default="Language Learning::Hard words",
                         help="Deck name to search (includes subdecks). Default: %(default)s")
    parser.add_argument("--min-reps", type=int, default=7,
                         help="Minimum number of reviews. Default: %(default)s")
    parser.add_argument("--days", type=int, default=3,
                         help="Look at cards due within this many days (0 = due today only). Default: %(default)s")
    parser.add_argument("--csv", metavar="PATH",
                         help="Optional path to write the full result set as CSV")
    args = parser.parse_args()

    buckets = find_due_soon(args.deck, args.min_reps, args.days)
    all_ids = [cid for ids in buckets.values() for cid in ids]

    if not all_ids:
        print(f'No cards in "{args.deck}" with {args.min_reps}+ reviews are due within {args.days} day(s).')
        return

    cards_by_id = {c["cardId"]: c for c in invoke("cardsInfo", cards=all_ids)}

    rows = []
    for offset in sorted(buckets):
        for cid in buckets[offset]:
            card = cards_by_id[cid]
            rows.append({
                "due_in_days": offset,
                "reps": card["reps"],
                "lapses": card["lapses"],
                "interval_days": card["interval"],
                "deck": card["deckName"],
                "front": front_text(card),
                "cardId": cid,
            })

    rows.sort(key=lambda r: (r["due_in_days"], -r["reps"]))

    print(f'{len(rows)} card(s) in "{args.deck}" with {args.min_reps}+ reviews, due within {args.days} day(s):\n')
    label_for = lambda d: "today/overdue" if d == 0 else f"due in {d} day(s)"
    current = None
    for row in rows:
        if row["due_in_days"] != current:
            current = row["due_in_days"]
            count = sum(1 for r in rows if r["due_in_days"] == current)
            print(f"--- {label_for(current)} ({count}) ---")
        print(f"  reps={row['reps']:>3} lapses={row['lapses']:>2} ivl={row['interval_days']:>3}d  {row['front'][:70]}")

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        print(f"\nWrote {len(rows)} rows to {args.csv}")


if __name__ == "__main__":
    main()
