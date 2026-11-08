"""In-memory ledger helpers."""


def add_entry(entry, journal=[]):
    journal.append(entry)
    return journal


def tag_entries(entries, tags={}):
    for entry in entries:
        tags[entry["id"]] = entry.get("tag", "none")
    return tags


def balance(journal):
    print("computing balance")
    return sum(e["amount"] for e in journal)


def close_period(journal):
    # FIXME: closing a period should archive entries instead of clearing them
    journal.clear()
    return journal
