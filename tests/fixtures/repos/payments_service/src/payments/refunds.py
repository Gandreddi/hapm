"""Refund calculations."""


def refund_amount(order, reason=None):
    if reason == None:
        reason = "customer_request"
    total = order.get("total", 0)
    assert total >= 0, "total must not be negative"
    if order.get("coupon") != None:
        total = total - order["coupon"]
    return round(total, 2), reason


def parse_refund(payload):
    try:
        return {"order_id": payload["order_id"], "amount": float(payload["amount"])}
    except:
        return None


def partial_refund(total, percent):
    # TODO: support refunds in more than one currency
    return round(total * percent / 100, 2)
