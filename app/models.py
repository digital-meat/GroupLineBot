from datetime import date, datetime

from sqlalchemy import Date, DateTime, Integer, String, Text, UniqueConstraint, func
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
    priority: Mapped[str] = mapped_column(String(8), default="medium")  # high / medium / low
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    tags: Mapped[str | None] = mapped_column(String(256), nullable=True)  # comma-separated
    source_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Summary(Base):
    """Cached summaries so we don't re-summarize the same range."""

    __tablename__ = "summaries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[str] = mapped_column(String(64), index=True)
    content: Mapped[str] = mapped_column(Text)
    message_from_id: Mapped[int] = mapped_column(Integer)  # oldest message.id covered
    message_to_id: Mapped[int] = mapped_column(Integer)  # newest message.id covered
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class GroupMember(Base):
    """Known members of a LINE group, auto-tracked from messages."""

    __tablename__ = "group_members"
    __table_args__ = (
        UniqueConstraint("group_id", "user_id", name="uq_group_member"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[str] = mapped_column(String(64))
    user_id: Mapped[str] = mapped_column(String(64))
    display_name: Mapped[str] = mapped_column(String(128))
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class GroupTag(Base):
    """Persistent tag vocabulary per group for LLM consistency."""

    __tablename__ = "group_tags"
    __table_args__ = (
        UniqueConstraint("group_id", "name", name="uq_group_tag"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class TokenUsage(Base):
    """Tracks LLM token usage per call."""

    __tablename__ = "token_usage"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(16))  # gemini / claude
    model: Mapped[str] = mapped_column(String(64))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    purpose: Mapped[str] = mapped_column(String(32))  # summary / chunk_summary
    group_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# ---------------------------------------------------------------------------
# Band practice review models
# ---------------------------------------------------------------------------

class PracticeSession(Base):
    """A single practice recording session (one WAV file)."""

    __tablename__ = "practice_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[str] = mapped_column(String(64), index=True)
    title: Mapped[str] = mapped_column(String(256))  # e.g. "2026-03-10 スタジオ練習"
    recorded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    duration_sec: Mapped[float | None] = mapped_column(nullable=True)
    drive_file_id: Mapped[str | None] = mapped_column(String(128), nullable=True)  # original WAV
    drive_mp3_id: Mapped[str | None] = mapped_column(String(128), nullable=True)   # mp3 for streaming
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending / processed / error
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class SongSegment(Base):
    """A detected or manually marked segment within a practice session."""

    __tablename__ = "song_segments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(Integer, index=True)
    track_number: Mapped[int] = mapped_column(Integer)
    title: Mapped[str | None] = mapped_column(String(256), nullable=True)  # song name (manual label)
    start_sec: Mapped[float] = mapped_column()
    end_sec: Mapped[float] = mapped_column()
    drive_file_id: Mapped[str | None] = mapped_column(String(128), nullable=True)  # split WAV
    drive_mp3_id: Mapped[str | None] = mapped_column(String(128), nullable=True)   # split mp3
    auto_detected: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Annotation(Base):
    """Timestamped annotation on a practice session by a band member."""

    __tablename__ = "annotations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(Integer, index=True)
    user_name: Mapped[str] = mapped_column(String(128))
    timestamp_sec: Mapped[float] = mapped_column()  # position in the session
    content: Mapped[str] = mapped_column(Text)
    annotation_type: Mapped[str] = mapped_column(String(16), default="comment")  # comment / good / issue
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ClipRequest(Base):
    """Request to clip a segment from a session — processed by Colab worker."""

    __tablename__ = "clip_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(Integer, index=True)
    start_sec: Mapped[float] = mapped_column()
    end_sec: Mapped[float] = mapped_column()
    title: Mapped[str | None] = mapped_column(String(256), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending / done / error
    drive_file_id: Mapped[str | None] = mapped_column(String(128), nullable=True)  # result file
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
