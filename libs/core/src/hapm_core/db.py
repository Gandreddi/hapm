"""Database engine helpers (SQLAlchemy 2, psycopg 3)."""

from sqlalchemy import Engine, create_engine


def make_engine(url: str) -> Engine:
    return create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=5)
