from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Message(Base):
    """Stores every message from the LINE group chat."""

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str] = mapped_column(String(64))
    display_name: Mapped[str] = mapped_column(String(128), default="Unknown")
    message_type: Mapped[str] = mapped_column(String(16))  # text, image, sticker, etc.
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    line_message_id: Mapped[str] = mapped_column(String(64), unique=True)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), index=True
    )


class Task(Base):
    """Tasks / TODOs extracted from chat by the LLM."""

    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[str] = mapped_column(String(64), index=True)
    title: Mapped[str] = mapped_column(String(256))
    assignee: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="open")  # open / done
    source_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Summary(Base):
    """Cached summaries so we don't re-summarize the same range."""

    __tablename__ = "summaries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[str] = mapped_column(String(64), index=True)
    content: Mapped[str] = mapped_column(Text)
    message_from_id: Mapped[int] = mapped_column(Integer)  # oldest message.id covered
    message_to_id: Mapped[int] = mapped_column(Integer)  # newest message.id covered
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
