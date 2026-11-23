"""Public HTTP API."""

from fastapi import APIRouter

from orders.service import OrderService

router = APIRouter()
service = OrderService()


@router.post("/orders")
def create_order(sku: str, quantity: int, price: str) -> dict[str, int]:
    order = service.create(sku, quantity, price)
    return {"total_cents": order.total_cents}


@router.get("/orders/{order_id}")
def get_order(order_id: int) -> dict[str, int]:
    return {"id": order_id}
