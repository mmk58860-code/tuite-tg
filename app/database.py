from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.orm import Session, declarative_base, sessionmaker


DATABASE_URL = os.getenv("TUITE_TG_DATABASE_URL", "sqlite:///./data/tuite_tg.db")

if DATABASE_URL.startswith("sqlite:///"):
    db_path = DATABASE_URL.replace("sqlite:///", "", 1)
    db_dir = os.path.dirname(db_path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class TokenInstance(Base):
    __tablename__ = "token_instances"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(120), nullable=False)
    rsshub_url = Column(String(500), nullable=False)
    auth_token = Column(Text, nullable=False, default="")
    ct0 = Column(String(300), nullable=False, default="")
    bearer_token = Column(Text, nullable=False, default="")
    proxy_url = Column(String(500), nullable=False, default="")
    enabled = Column(Boolean, nullable=False, default=True)
    healthy = Column(Boolean, nullable=False, default=True)
    use_fallback = Column(Boolean, nullable=False, default=False)
    graphql_query_id = Column(String(120), nullable=False, default="")
    last_error = Column(Text, nullable=False, default="")
    last_checked_at = Column(DateTime(timezone=True), nullable=True)
    last_success_at = Column(DateTime(timezone=True), nullable=True)
    last_alerted_at = Column(DateTime(timezone=True), nullable=True)
    last_repaired_at = Column(DateTime(timezone=True), nullable=True)
    cooldown_until = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)


class WatchList(Base):
    __tablename__ = "watch_lists"
    __table_args__ = (UniqueConstraint("token_id", "list_id", name="uq_token_list"),)

    id = Column(Integer, primary_key=True, index=True)
    token_id = Column(Integer, nullable=False, index=True)
    name = Column(String(120), nullable=False, default="")
    list_id = Column(String(80), nullable=False)
    enabled = Column(Boolean, nullable=False, default=True)
    subscription_checked_at = Column(DateTime(timezone=True), nullable=True)
    subscription_error = Column(Text, nullable=False, default="")
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)


class TokenListState(Base):
    __tablename__ = "token_list_states"
    __table_args__ = (UniqueConstraint("token_id", "watch_list_id", name="uq_token_list_state"),)

    id = Column(Integer, primary_key=True, index=True)
    token_id = Column(Integer, nullable=False, index=True)
    watch_list_id = Column(Integer, nullable=False, index=True)
    subscription_checked_at = Column(DateTime(timezone=True), nullable=True)
    subscription_error = Column(Text, nullable=False, default="")
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)


class RsshubInstance(Base):
    __tablename__ = "rsshub_instances"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(120), unique=True, nullable=False, index=True)
    host_port = Column(Integer, unique=True, nullable=False, index=True)
    internal_url = Column(String(300), nullable=False)
    twitter_auth_token = Column(Text, nullable=False, default="")
    third_party_api = Column(Text, nullable=False, default="")
    proxy_uri = Column(String(500), nullable=False, default="")
    container_id = Column(String(120), nullable=False, default="")
    status = Column(String(40), nullable=False, default="unknown")
    last_test_at = Column(DateTime(timezone=True), nullable=True)
    last_test_ok = Column(Boolean, nullable=False, default=False)
    last_test_message = Column(Text, nullable=False, default="")
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)


class SeenItem(Base):
    __tablename__ = "seen_items"

    id = Column(Integer, primary_key=True, index=True)
    item_id = Column(String(300), unique=True, nullable=False, index=True)
    list_id = Column(String(80), nullable=False, index=True)
    token_id = Column(Integer, nullable=False, index=True)
    title = Column(Text, nullable=False, default="")
    link = Column(Text, nullable=False, default="")
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    forwarded_at = Column(DateTime(timezone=True), nullable=True)


class UserAlias(Base):
    __tablename__ = "user_aliases"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(120), unique=True, nullable=False, index=True)
    note = Column(String(200), nullable=False, default="")
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)


class Setting(Base):
    __tablename__ = "settings"

    key = Column(String(120), primary_key=True)
    value = Column(Text, nullable=False, default="")


class Log(Base):
    __tablename__ = "logs"

    id = Column(Integer, primary_key=True, index=True)
    level = Column(String(20), nullable=False)
    message = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    ensure_schema_migrations()


def ensure_schema_migrations() -> None:
    if not DATABASE_URL.startswith("sqlite"):
        return
    with engine.begin() as conn:
        token_columns = {
            row[1]
            for row in conn.exec_driver_sql("PRAGMA table_info(token_instances)").fetchall()
        }
        if "last_success_at" not in token_columns:
            conn.exec_driver_sql("ALTER TABLE token_instances ADD COLUMN last_success_at DATETIME")
        if "last_alerted_at" not in token_columns:
            conn.exec_driver_sql("ALTER TABLE token_instances ADD COLUMN last_alerted_at DATETIME")

        list_columns = {
            row[1]
            for row in conn.exec_driver_sql("PRAGMA table_info(watch_lists)").fetchall()
        }
        if "subscription_checked_at" not in list_columns:
            conn.exec_driver_sql("ALTER TABLE watch_lists ADD COLUMN subscription_checked_at DATETIME")
        if "subscription_error" not in list_columns:
            conn.exec_driver_sql("ALTER TABLE watch_lists ADD COLUMN subscription_error TEXT NOT NULL DEFAULT ''")
        conn.exec_driver_sql(
            """
            DELETE FROM watch_lists
            WHERE id NOT IN (
                SELECT MIN(id)
                FROM watch_lists
                GROUP BY list_id
            )
            """
        )
        conn.exec_driver_sql("UPDATE watch_lists SET token_id = 0 WHERE token_id != 0")

        rsshub_columns = {
            row[1]
            for row in conn.exec_driver_sql("PRAGMA table_info(rsshub_instances)").fetchall()
        }
        if "last_test_at" not in rsshub_columns:
            conn.exec_driver_sql("ALTER TABLE rsshub_instances ADD COLUMN last_test_at DATETIME")
        if "last_test_ok" not in rsshub_columns:
            conn.exec_driver_sql("ALTER TABLE rsshub_instances ADD COLUMN last_test_ok BOOLEAN NOT NULL DEFAULT 0")
        if "last_test_message" not in rsshub_columns:
            conn.exec_driver_sql("ALTER TABLE rsshub_instances ADD COLUMN last_test_message TEXT NOT NULL DEFAULT ''")


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def get_setting(db: Session, key: str, default: str = "") -> str:
    row = db.query(Setting).filter(Setting.key == key).first()
    return row.value if row else default


def set_setting(db: Session, key: str, value: str) -> None:
    row = db.query(Setting).filter(Setting.key == key).first()
    if row:
        row.value = value
    else:
        db.add(Setting(key=key, value=value))


def add_log(db: Session, level: str, message: str) -> None:
    db.add(Log(level=level.upper(), message=message[:2000]))
