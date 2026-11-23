"""Inventory HTTP API."""

from fastapi import FastAPI

from inventory.stock import available, reserve

app = FastAPI()


@app.get("/inventory/{sku}")
def get_stock(sku: str) -> dict[str, int]:
    return {"available": available(sku)}


@app.post("/inventory/{sku}/reservations")
def create_reservation(sku: str, quantity: int) -> dict[str, str]:
    reserve(sku, quantity)
    return {"status": "reserved"}
