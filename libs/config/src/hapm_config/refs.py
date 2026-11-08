"""Secret references: env:NAME reads an environment variable; kv:NAME reads an Azure Key Vault secret.

Key Vault secrets are mounted as files by the Secrets Store CSI driver in AKS (HAPM_KV_MOUNT,
default /mnt/secrets); locally they can be supplied as environment variables named KV_<NAME>.
"""

import os
import re
from pathlib import Path

_REF = re.compile(r"^(env|kv):([A-Za-z0-9_\-.]+)$")


class RefError(ValueError):
    pass


def is_ref(value: str) -> bool:
    return bool(_REF.match(value))


def resolve_ref(ref: str | None, *, required: bool = True) -> str | None:
    """Return the secret value, or None when it is unset and not required."""
    if not ref:
        if required:
            raise RefError("missing reference")
        return None
    match = _REF.match(ref)
    if not match:
        raise RefError(f"not a reference (use env:NAME or kv:NAME): {ref[:8]}...")
    kind, name = match.groups()
    value: str | None
    if kind == "env":
        value = os.environ.get(name)
    else:
        mounted = Path(os.environ.get("HAPM_KV_MOUNT", "/mnt/secrets")) / name
        value = (
            mounted.read_text(encoding="utf-8").strip()
            if mounted.is_file()
            else os.environ.get("KV_" + name.upper().replace("-", "_"))
        )
    if not value:
        if required:
            raise RefError(f"{ref} is not set")
        return None
    return value
