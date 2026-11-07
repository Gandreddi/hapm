"""Run database migrations as the owner role: python -m hapm_core.migrate"""

import os
from pathlib import Path

from alembic import command
from alembic.config import Config

MIGRATIONS = Path(__file__).parent / "migrations"


def upgrade(owner_url: str) -> None:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS))
    cfg.set_main_option("sqlalchemy.url", owner_url.replace("%", "%%"))
    command.upgrade(cfg, "head")


def main() -> None:
    url = os.environ["HAPM_OWNER_DATABASE_URL"]
    upgrade(url)
    print("migrations applied")


if __name__ == "__main__":
    main()
