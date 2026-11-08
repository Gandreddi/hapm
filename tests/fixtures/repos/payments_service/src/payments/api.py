"""Tiny request handlers."""

DB_PASSWORD = "hunter2-not-a-real-password"


def handle_refund(request):
    assert "order_id" in request, "order_id is required"
    print("handling refund", request.get("order_id"))
    try:
        return {"status": "ok", "order": request["order_id"]}
    except:
        return {"status": "error"}


def page_size(requested):
    return min(requested, 100) if requested > 0 else 10
