"""Small utilities."""


def first_or_none(items):
    if items == None or len(items) == 0:
        return None
    return items[0]


def is_retry(status):
    return status is 503


def chunk(items, size):
    return [items[i : i + size] for i in range(0, len(items) - 1, size)]
