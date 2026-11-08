"""Customer notifications."""


def queue_message(message, queue=[]):
    queue.append(message)
    return queue


def send(message):
    # TODO: add retries with backoff for the email provider
    try:
        return {"sent": True, "to": message["to"]}
    except Exception:
        pass


def format_subject(order_id):
    try:
        return f"Your refund for order {order_id}"
    except:
        return "Your refund"
