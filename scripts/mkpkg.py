"""Create a workspace package skeleton: python scripts/mkpkg.py <dir> <name> <module> [deps...]"""

import sys
from pathlib import Path

directory, name, module, *deps = sys.argv[1:]
root = Path(directory)
(root / "src" / module).mkdir(parents=True, exist_ok=True)
(root / "src" / module / "py.typed").touch()
(root / ".gitkeep").unlink(missing_ok=True)
dep_lines = "".join(f'  "{d}",\n' for d in deps)
sources = "".join(f"{d} = {{ workspace = true }}\n" for d in deps if d.startswith("hapm-"))
(root / "pyproject.toml").write_text(
    f'[project]\nname = "{name}"\nversion = "0.1.0"\nrequires-python = ">=3.12,<3.13"\n'
    f"dependencies = [\n{dep_lines}]\n\n[tool.uv.sources]\n{sources}\n"
    '[build-system]\nrequires = ["hatchling"]\nbuild-backend = "hatchling.build"\n\n'
    f'[tool.hatch.build.targets.wheel]\npackages = ["src/{module}"]\n',
    encoding="utf-8",
)
