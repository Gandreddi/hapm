"""Canonical node ids. Every engine must produce the same id for the same entity so results merge."""

import re


def repo_id(repo: str) -> str:
    return f"repo:{repo}"


def service_id(repo: str) -> str:
    return f"svc:{repo}"


def module_id(repo: str, path: str) -> str:
    return f"{repo}:{path}"


def symbol_id(repo: str, path: str, qualname: str) -> str:
    return f"{repo}:{path}#{qualname}"


def lib_id(name: str) -> str:
    return f"lib:{name.lower()}"


def topic_id(name: str) -> str:
    return f"topic:{name}"


def table_id(name: str) -> str:
    return f"table:{name.lower()}"


def ticket_id(key: str) -> str:
    return f"ticket:{key.upper()}"


def capability_id(name: str) -> str:
    return "cap:" + re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def endpoint_id(repo: str, method: str, path: str) -> str:
    return f"{repo}:api#{method.upper()} {normalise_route(path)}"


def normalise_route(path: str) -> str:
    """'/orders/<int:id>' and '/orders/:id' both become '/orders/{id}'; trailing slash removed."""
    path = re.sub(r"<(?:[a-z]+:)?([A-Za-z_][A-Za-z0-9_]*)>", r"{\1}", path)
    path = re.sub(r":([A-Za-z_][A-Za-z0-9_]*)", r"{\1}", path)
    path = "/" + path.strip("/")
    return path


def route_pattern(path: str) -> re.Pattern[str]:
    """A regex matching concrete or templated calls against a route template."""
    parts = []
    for segment in normalise_route(path).strip("/").split("/"):
        parts.append(r"[^/]+" if segment.startswith("{") and segment.endswith("}") else re.escape(segment))
    return re.compile("^/" + "/".join(parts) + "$")


def split_id(node_id: str) -> tuple[str, str, str]:
    """(repo, path, qualname) for code ids; ('', '', id) for global ids."""
    if node_id.split(":", 1)[0] in ("repo", "svc", "lib", "topic", "table", "ticket", "cap"):
        return "", "", node_id
    repo, rest = node_id.split(":", 1)
    path, _, qual = rest.partition("#")
    return repo, path, qual
