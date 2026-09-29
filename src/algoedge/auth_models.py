"""ORM mappings for the two pre-existing authentication tables.

`users` and `user_login_activity` already exist in the application database
(created by the operator, not by this app). They are mapped on their OWN
declarative base, never on algoedge.models.Base, so db.init_db()'s
Base.metadata.create_all() can never create or alter them - the app only
reads and writes rows. Column types mirror the existing schema exactly
(NVARCHAR, DATETIME2, BIT, BIGINT identity); the SQLite variants exist only
so tests can build a throwaway copy of the schema.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, Unicode
from sqlalchemy.dialects import mssql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# DATETIME2 on SQL Server (the existing columns' type); plain DateTime elsewhere (tests).
_DATETIME2 = DateTime().with_variant(mssql.DATETIME2(), "mssql")
# BIGINT IDENTITY on SQL Server; SQLite only auto-increments an INTEGER primary key.
_BIGINT_ID = BigInteger().with_variant(Integer(), "sqlite")


class AuthBase(DeclarativeBase):
    """Separate metadata: nothing in the application ever calls create_all() on it."""


class User(AuthBase):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str | None] = mapped_column(Unicode(254), nullable=True, unique=True)
    mobile_no: Mapped[str | None] = mapped_column(Unicode(20), nullable=True, unique=True)
    password_hash: Mapped[str] = mapped_column(Unicode(500), nullable=False)
    role: Mapped[str] = mapped_column(Unicode(50), nullable=False, default="USER")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    failed_login_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(_DATETIME2, nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(_DATETIME2, nullable=True)
    created_at: Mapped[datetime] = mapped_column(_DATETIME2, nullable=False)
    updated_at: Mapped[datetime | None] = mapped_column(_DATETIME2, nullable=True)


class UserLoginActivity(AuthBase):
    __tablename__ = "user_login_activity"

    id: Mapped[int] = mapped_column(_BIGINT_ID, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True)
    login_identifier: Mapped[str | None] = mapped_column(Unicode(254), nullable=True)
    identifier_type: Mapped[str | None] = mapped_column(Unicode(20), nullable=True)
    attempt_at: Mapped[datetime] = mapped_column(_DATETIME2, nullable=False)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    failure_reason: Mapped[str | None] = mapped_column(Unicode(50), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(Unicode(45), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Unicode(1000), nullable=True)
    session_id: Mapped[str | None] = mapped_column(Unicode(100), nullable=True)
