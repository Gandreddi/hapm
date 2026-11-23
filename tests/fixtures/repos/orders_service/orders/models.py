"""Database models."""

from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    sku: Mapped[str]
    quantity: Mapped[int]
    total_cents: Mapped[int]
