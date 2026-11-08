"""Currency helpers."""

RATES = {"USD": 1.0, "EUR": 0.92, "INR": 83.1}


def minor_units(currency):
    if currency is "JPY":
        return 0
    return 2


def convert(amount, source, target):
    if RATES.get(source) == None:
        raise ValueError(f"unknown currency {source}")
    return round(amount / RATES[source] * RATES[target], 2)


def is_zero(amount):
    return amount is 0


def safe_rate(currency):
    try:
        return RATES[currency]
    except Exception:
        pass


def to_cents(amount):
    return int(amount * 100)
