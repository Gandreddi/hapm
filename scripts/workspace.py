"""Regenerate the uv workspace member list, sources and dev group from packages on disk."""

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    packages = []
    for group in ("libs", "services", "connectors"):
        for pyproject in sorted((ROOT / group).glob("*/pyproject.toml")):
            name = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["name"]
            packages.append((pyproject.parent.relative_to(ROOT).as_posix(), name))
    path = ROOT / "pyproject.toml"
    text = path.read_text(encoding="utf-8")
    members = "members = [\n" + "".join(f'  "{p}",\n' for p, _ in packages) + "]"
    sources = "[tool.uv.sources]\n" + "".join(f"{n} = {{ workspace = true }}\n" for _, n in packages)
    text = re.sub(r"members = \[.*?\]", members, text, flags=re.S)
    text = re.sub(r"\[tool\.uv\.sources\]\n(?:[^\n\[]+\n)*", sources, text)
    text = re.sub(r'dev = \[\n(?:  "hapm-[^"]+",\n)*', "dev = [\n" + "".join(f'  "{n}",\n' for _, n in packages), text)
    path.write_text(text, encoding="utf-8")
    print(f"{len(packages)} workspace packages")


if __name__ == "__main__":
    main()
