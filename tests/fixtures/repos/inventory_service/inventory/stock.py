"""Stock bookkeeping."""

from acme_shared.events import subscribe

STOCK: dict[str, int] = {}


def available(sku: str) -> int:
    return STOCK.get(sku, 0)


def reserve(sku: str, quantity: int) -> None:
    STOCK[sku] = available(sku) - quantity


@subscribe("orders.created")
def on_order_created(event: dict) -> None:  # type: ignore[type-arg]
    """Keep a log of created orders."""
    LOG_SQL = "INSERT INTO inventory_log (sku, quantity) VALUES (:sku, :quantity)"
    print(LOG_SQL, event)
