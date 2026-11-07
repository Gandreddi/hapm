import uuid


def new_id(prefix: str) -> str:
    """Readable unique id such as run_3f2a...; the prefix shows what kind of object it is."""
    return f"{prefix}_{uuid.uuid4().hex}"
