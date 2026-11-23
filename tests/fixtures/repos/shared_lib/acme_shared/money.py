"""Money helpers shared by every service."""

from decimal import ROUND_HALF_UP, Decimal


class Money:
    """An amount in minor units with a currency code."""

    def __init__(self, cents: int, currency: str = "EUR") -> None:
        self.cents = cents
        self.currency = currency

    def add(self, other: "Money") -> "Money":
        if other.currency != self.currency:
            raise ValueError("currency mismatch")
        return Money(self.cents + other.cents, self.currency)


def to_cents(amount: str) -> int:
    """Convert a decimal string such as '12.34' to cents, rounding half up."""
    return int((Decimal(amount) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
