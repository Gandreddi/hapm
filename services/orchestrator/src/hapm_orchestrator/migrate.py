"""Owner-role setup: Alembic migrations, then LangGraph checkpoint tables."""

import os

from hapm_core.migrate import upgrade
from langgraph.checkpoint.postgres import PostgresSaver


def main() -> None:
    owner_url = os.environ["HAPM_OWNER_DATABASE_URL"]
    upgrade(owner_url)
    with PostgresSaver.from_conn_string(owner_url.replace("postgresql+psycopg://", "postgresql://", 1)) as saver:
        saver.setup()
    print("migrations and checkpoint tables applied")


if __name__ == "__main__":
    main()
