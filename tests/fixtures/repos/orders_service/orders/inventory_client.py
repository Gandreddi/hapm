"""HTTP client for the inventory service."""

import httpx

from orders.config import INVENTORY_URL


def stock_level(sku: str) -> int:
    """Return how many items of sku are in stock."""
    resp = httpx.get(f"{INVENTORY_URL}/inventory/{sku}", timeout=5)
    resp.raise_for_status()
    return int(resp.json()["available"])


def reserve(sku: str, quantity: int) -> None:
    """Reserve stock for an order."""
    httpx.post(f"{INVENTORY_URL}/inventory/{sku}/reservations", json={"quantity": quantity}, timeout=5).raise_for_status()
