"""GroupLineBot – LINE group chat summarizer powered by LLM."""

import logging
import random
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.webhooks import (
    JoinEvent,
    MessageEvent,
    TextMessageContent,
    UnsendEvent,
)
from linebot.v3.webhook import WebhookParser
from sqlalchemy import cast, delete, func, select, Date
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.config import (
    APP_URL,
    DATA_RETENTION_DAYS,
    LINE_CHANNEL_SECRET,
    MESSAGE_RETENTION_COUNT,
    SUMMARY_MESSAGE_LIMIT,
)
from app.database import async_session, init_db
from app.line_client import get_api, get_display_name, reply_text
from app.models import GroupMember, GroupTag, Message, Summary, Task, TokenUsage
from app.summarizer import summarize_chat

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

parser = WebhookParser(LINE_CHANNEL_SECRET)

app = FastAPI(title="GroupLineBot")

_html_cache: dict[str, str] = {}


def _get_html(name: str) -> str:
    if name not in _html_cache:
        _html_cache[name] = (Path(__file__).parent / name).read_text(encoding="utf-8")
    return _html_cache[name]


# ---------------------------------------------------------------------------
# Webhook endpoint
# ---------------------------------------------------------------------------
@app.post("/callback")
async def callback(
    request: Request,
    x_line_signature: str = Header(...),
):
    await init_db()

    body = (await request.body()).decode("utf-8")

    try:
        events = parser.parse(body, x_line_signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature")

    api = get_api()

    for event in events:
        if isinstance(event, JoinEvent):
            logger.info("Bot joined group: %s", event.source.group_id)

        elif isinstance(event, UnsendEvent):
            await handle_unsend(event)

        elif isinstance(event, MessageEvent):
            await handle_message(api, event)

    return "OK"


# ---------------------------------------------------------------------------
# Unsend handling – delete message from DB when user unsends
# ---------------------------------------------------------------------------
async def handle_unsend(event: UnsendEvent):
    """Remove the unsent message from the database."""
    message_id = event.unsend.message_id
    async with async_session() as session:
        result = await session.execute(
            delete(Message).where(Message.line_message_id == str(message_id))
        )
        await session.commit()
        if result.rowcount:
            logger.info("Deleted unsent message %s", message_id)
        else:
            logger.info("Unsent message %s not found in DB (already deleted or not stored)", message_id)


# ---------------------------------------------------------------------------
# Data retention – automatic pruning
# ---------------------------------------------------------------------------

async def _prune_old_data(group_id: str) -> None:
    """Delete old messages/summaries/token_usage to cap DB size.

    Called probabilistically (~10% of message inserts) to avoid
    running expensive DELETEs on every single request.
    """
    async with async_session() as session:
        # 1) Messages: keep only the latest MESSAGE_RETENTION_COUNT per group
        if MESSAGE_RETENTION_COUNT > 0:
            # Find the id threshold: keep rows with id >= cutoff
            cutoff_stmt = (
                select(Message.id)
                .where(Message.group_id == group_id)
                .order_by(Message.id.desc())
                .offset(MESSAGE_RETENTION_COUNT)
                .limit(1)
            )
            cutoff_result = await session.execute(cutoff_stmt)
            cutoff_id = cutoff_result.scalar_one_or_none()
            if cutoff_id is not None:
                del_msg = await session.execute(
                    delete(Message)
                    .where(Message.group_id == group_id)
                    .where(Message.id <= cutoff_id)
                )
                if del_msg.rowcount:
                    logger.info(
                        "Pruned %d old messages for group %s",
                        del_msg.rowcount, group_id,
                    )

        # 2) Summaries & token_usage: delete records older than DATA_RETENTION_DAYS
        if DATA_RETENTION_DAYS > 0:
            cutoff_date = datetime.utcnow() - timedelta(days=DATA_RETENTION_DAYS)

            del_sum = await session.execute(
                delete(Summary)
                .where(Summary.group_id == group_id)
                .where(Summary.created_at < cutoff_date)
            )
            if del_sum.rowcount:
                logger.info(
                    "Pruned %d old summaries for group %s",
                    del_sum.rowcount, group_id,
                )

            del_usage = await session.execute(
                delete(TokenUsage)
                .where(TokenUsage.group_id == group_id)
                .where(TokenUsage.created_at < cutoff_date)
            )
            if del_usage.rowcount:
                logger.info(
                    "Pruned %d old token_usage records for group %s",
                    del_usage.rowcount, group_id,
                )

        await session.commit()


# ---------------------------------------------------------------------------
# Time argument parsing for /summary
# ---------------------------------------------------------------------------
JST = timezone(timedelta(hours=9))

# Patterns:
#   "14:00"          → today at 14:00 JST
#   "2/19 14:00"     → Feb 19 at 14:00 JST (current year)
#   "2025/2/19 14:00" → Feb 19 2025 at 14:00 JST
#   "3時間"          → 3 hours ago
#   "30分"           → 30 minutes ago

_RE_DURATION = re.compile(r"^(\d+)\s*(時間|分)$")
_RE_DATE_TIME = re.compile(
    r"^(?:(\d{4})/)?(\d{1,2})/(\d{1,2})\s+(\d{1,2}):(\d{2})$"
)
_RE_TIME_ONLY = re.compile(r"^(\d{1,2}):(\d{2})$")


def _parse_time_arg(arg: str) -> datetime | None:
    """Parse a Japanese-friendly time argument into a JST-aware datetime."""
    arg = arg.strip()
    now = datetime.now(JST)

    m = _RE_DURATION.match(arg)
    if m:
        val = int(m.group(1))
        unit = m.group(2)
        if unit == "時間":
            return now - timedelta(hours=val)
        else:  # 分
            return now - timedelta(minutes=val)

    m = _RE_DATE_TIME.match(arg)
    if m:
        year = int(m.group(1)) if m.group(1) else now.year
        month, day = int(m.group(2)), int(m.group(3))
        hour, minute = int(m.group(4)), int(m.group(5))
        try:
            return datetime(year, month, day, hour, minute, tzinfo=JST)
        except ValueError:
            return None

    m = _RE_TIME_ONLY.match(arg)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
        try:
            dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if dt > now:
                dt -= timedelta(days=1)
            return dt
        except ValueError:
            return None

    return None


# ---------------------------------------------------------------------------
# Message handler
# ---------------------------------------------------------------------------
async def handle_message(api, event: MessageEvent):
    group_id = getattr(event.source, "group_id", None)
    if not group_id:
        return  # Only handle group messages

    # Only handle text messages – skip images, videos, stickers, etc.
    if not isinstance(event.message, TextMessageContent):
        return

    user_id = event.source.user_id or "unknown"
    display_name = await get_display_name(api, group_id, user_id)
    content = event.message.text

    # Store message + upsert group member
    async with async_session() as session:
        msg = Message(
            group_id=group_id,
            user_id=user_id,
            display_name=display_name,
            message_type="text",
            content=content,
            line_message_id=event.message.id,
        )
        session.add(msg)
        await session.execute(
            pg_insert(GroupMember)
            .values(group_id=group_id, user_id=user_id, display_name=display_name)
            .on_conflict_do_update(
                constraint="uq_group_member",
                set_={"display_name": display_name, "updated_at": func.now()},
            )
        )
        await session.commit()

    # Probabilistic pruning (~10% of inserts) to cap DB size
    if random.random() < 0.1:
        try:
            await _prune_old_data(group_id)
        except Exception:
            logger.exception("Pruning failed for group %s", group_id)

    # Check for bot commands
    if content:
        cmd = content.strip().strip("\u3000")
        logger.info("Received cmd=%r from group=%s", cmd, group_id)
        # /summary, /まとめ, 要約して, まとめ – with optional time arg
        # Accepts: "まとめ3時間", "まとめ 14:00", "/まとめ 2/19 14:00", etc.
        summary_match = re.match(
            r"^(/summary|要約して|/?まとめ)[\s　]*(.+)?$", cmd
        )
        if summary_match:
            time_arg = (summary_match.group(2) or "").strip() or None
            since = _parse_time_arg(time_arg) if time_arg else None
            if time_arg and since is None:
                reply_text(
                    api,
                    event,
                    "時刻の形式が不正です。例: まとめ14:00, まとめ3時間, まとめ2/19 14:00",
                )
                return
            await cmd_summary(api, event, group_id, since=since)
        elif cmd in ("/tasks", "タスク一覧", "/タスク"):
            await cmd_tasks(api, event, group_id)
        elif cmd.startswith("/done "):
            await cmd_done_task(api, event, group_id, cmd)
        elif cmd == "/clear-tasks":
            await cmd_clear_tasks(api, event, group_id)
        elif cmd == "/clear-messages":
            await cmd_clear_messages(api, event, group_id)
        elif cmd == "/clear-all":
            await cmd_clear_all(api, event, group_id)


# ---------------------------------------------------------------------------
# /summary – Summarize recent chat
# ---------------------------------------------------------------------------
async def cmd_summary(
    api, event: MessageEvent, group_id: str, *, since: datetime | None = None
):
    time_specified = since is not None

    async with async_session() as session:
        # Find the latest summary so we only summarize new messages
        last_summary_stmt = (
            select(Summary)
            .where(Summary.group_id == group_id)
            .order_by(Summary.id.desc())
            .limit(1)
        )
        last_summary_result = await session.execute(last_summary_stmt)
        last_summary = last_summary_result.scalar_one_or_none()

        stmt = (
            select(Message)
            .where(Message.group_id == group_id)
            .where(Message.message_type == "text")
            .where(Message.content.isnot(None))
        )
        if time_specified:
            # DB stores naive UTC timestamps; convert aware JST → naive UTC
            since_utc = since.astimezone(timezone.utc).replace(tzinfo=None)
            stmt = stmt.where(Message.timestamp >= since_utc)
        elif last_summary is not None:
            stmt = stmt.where(Message.id > last_summary.message_to_id)
        stmt = stmt.order_by(Message.id.desc()).limit(SUMMARY_MESSAGE_LIMIT)

        result = await session.execute(stmt)
        rows = result.scalars().all()

        # Fetch known members and tags for LLM context
        members_result = await session.execute(
            select(GroupMember.display_name)
            .where(GroupMember.group_id == group_id)
            .order_by(GroupMember.display_name)
        )
        known_members = [r[0] for r in members_result.all()]

        tags_result = await session.execute(
            select(GroupTag.name)
            .where(GroupTag.group_id == group_id)
            .order_by(GroupTag.name)
        )
        known_tags = [r[0] for r in tags_result.all()]

    if not rows:
        if time_specified:
            since_str = since.astimezone(JST).strftime("%-m/%d %H:%M")
            reply_text(api, event, f"{since_str} 以降のメッセージがありません。")
        else:
            reply_text(api, event, "新しいメッセージがありません（前回の要約以降）。")
        return

    # When a time range is specified, don't carry over previous summary context
    previous_summary_text = (
        None if time_specified else (last_summary.content if last_summary else None)
    )

    rows = list(reversed(rows))  # chronological order

    messages = [
        {
            "display_name": r.display_name,
            "content": r.content,
            "message_type": r.message_type,
            "timestamp": r.timestamp,
        }
        for r in rows
    ]

    try:
        result = summarize_chat(
            messages,
            previous_summary=previous_summary_text,
            known_members=known_members,
            known_tags=known_tags,
        )
    except Exception as e:
        logger.exception("Summarization failed")
        reply_text(api, event, f"要約に失敗しました: {e}")
        return

    # Save tasks
    async with async_session() as session:
        for t in result.get("tasks", []):
            tags = t.get("tags")
            task = Task(
                group_id=group_id,
                title=t["title"],
                assignee=t.get("assignee"),
                tags=",".join(tags) if tags else None,
                source_summary=result["summary"][:500],
            )
            session.add(task)

        # Save new tags to group vocabulary
        all_tags: set[str] = set()
        for t in result.get("tasks", []):
            for tag in t.get("tags") or []:
                all_tags.add(tag)
        for tag_name in all_tags:
            await session.execute(
                pg_insert(GroupTag)
                .values(group_id=group_id, name=tag_name)
                .on_conflict_do_nothing(constraint="uq_group_tag")
            )

        # Save summary record
        summary_record = Summary(
            group_id=group_id,
            content=result["summary"],
            message_from_id=rows[0].id,
            message_to_id=rows[-1].id,
        )
        session.add(summary_record)

        # Save token usage
        for u in result.get("token_usage", []):
            session.add(TokenUsage(
                provider=u.get("provider", ""),
                model=u.get("model", ""),
                input_tokens=u.get("input_tokens", 0),
                output_tokens=u.get("output_tokens", 0),
                purpose=u.get("purpose", "summary"),
                group_id=group_id,
            ))

        await session.commit()

    # Build response
    text = result["summary"]
    if result.get("tasks") and APP_URL:
        text += f"\n\nタスク管理: {APP_URL}/tasks/view?group_id={group_id}"

    reply_text(api, event, text)


# ---------------------------------------------------------------------------
# /tasks – Show open tasks
# ---------------------------------------------------------------------------
async def cmd_tasks(api, event: MessageEvent, group_id: str):
    async with async_session() as session:
        stmt = (
            select(Task)
            .where(Task.group_id == group_id)
            .where(Task.status == "open")
            .order_by(Task.created_at.desc())
            .limit(30)
        )
        result = await session.execute(stmt)
        tasks = result.scalars().all()

    if not tasks:
        reply_text(api, event, "未完了のタスクはありません")
        return

    text = "未完了タスク一覧"
    for t in tasks:
        assignee = f" (@{t.assignee})" if t.assignee else ""
        text += f"\n#{t.id} {t.title}{assignee}"
    text += "\n\n完了するには: /done [番号]"
    if APP_URL:
        text += f"\nタスク管理: {APP_URL}/t?g={group_id}"

    reply_text(api, event, text)


# ---------------------------------------------------------------------------
# /done – Mark a task as done
# ---------------------------------------------------------------------------
async def cmd_done_task(api, event: MessageEvent, group_id: str, cmd: str):
    try:
        task_id = int(cmd.split()[1])
    except (IndexError, ValueError):
        reply_text(api, event, "使い方: /done [タスク番号]")
        return

    async with async_session() as session:
        stmt = select(Task).where(Task.id == task_id, Task.group_id == group_id)
        result = await session.execute(stmt)
        task = result.scalar_one_or_none()

        if not task:
            reply_text(api, event, f"タスク #{task_id} が見つかりません。")
            return

        task.status = "done"
        task.completed_at = datetime.utcnow()
        await session.commit()

    reply_text(api, event, f"✅ タスク #{task_id} を完了にしました: {task.title}")


# ---------------------------------------------------------------------------
# /clear-tasks – Delete all tasks for this group
# ---------------------------------------------------------------------------
async def cmd_clear_tasks(api, event: MessageEvent, group_id: str):
    async with async_session() as session:
        result = await session.execute(
            delete(Task).where(Task.group_id == group_id)
        )
        await session.commit()
    reply_text(api, event, f"🗑️ タスクを {result.rowcount} 件削除しました。")


# ---------------------------------------------------------------------------
# /clear-messages – Delete all stored messages for this group
# ---------------------------------------------------------------------------
async def cmd_clear_messages(api, event: MessageEvent, group_id: str):
    async with async_session() as session:
        result = await session.execute(
            delete(Message).where(Message.group_id == group_id)
        )
        await session.commit()
    reply_text(api, event, f"🗑️ メッセージ履歴を {result.rowcount} 件削除しました。")


# ---------------------------------------------------------------------------
# /clear-all – Delete all data (messages, tasks, summaries) for this group
# ---------------------------------------------------------------------------
async def cmd_clear_all(api, event: MessageEvent, group_id: str):
    async with async_session() as session:
        r_msg = await session.execute(
            delete(Message).where(Message.group_id == group_id)
        )
        r_task = await session.execute(
            delete(Task).where(Task.group_id == group_id)
        )
        r_sum = await session.execute(
            delete(Summary).where(Summary.group_id == group_id)
        )
        await session.commit()
    reply_text(
        api,
        event,
        f"🗑️ 全データ削除完了\n"
        f"  メッセージ: {r_msg.rowcount} 件\n"
        f"  タスク: {r_task.rowcount} 件\n"
        f"  要約: {r_sum.rowcount} 件",
    )


# ---------------------------------------------------------------------------
# REST API – Task management for web UI
# ---------------------------------------------------------------------------
@app.post("/api/tasks")
async def api_create_task(request: Request):
    await init_db()
    body = await request.json()
    group_id = body.get("group_id")
    title = (body.get("title") or "").strip()
    if not group_id or not title:
        raise HTTPException(status_code=400, detail="group_id and title are required")
    tags_list = body.get("tags")
    async with async_session() as session:
        task = Task(
            group_id=group_id,
            title=title,
            assignee=body.get("assignee"),
            tags=",".join(tags_list) if tags_list else None,
        )
        session.add(task)
        await session.commit()
        await session.refresh(task)
    return {
        "id": task.id,
        "title": task.title,
        "assignee": task.assignee,
        "status": task.status,
        "tags": task.tags.split(",") if task.tags else [],
        "created_at": task.created_at.isoformat() + "Z" if task.created_at else None,
        "completed_at": None,
    }


@app.get("/api/tasks")
async def api_list_tasks(group_id: str = Query(...)):
    await init_db()
    async with async_session() as session:
        stmt = (
            select(Task)
            .where(Task.group_id == group_id)
            .order_by(Task.status.asc(), Task.created_at.desc())
        )
        result = await session.execute(stmt)
        tasks = result.scalars().all()
    return [
        {
            "id": t.id,
            "title": t.title,
            "assignee": t.assignee,
            "status": t.status,
            "tags": t.tags.split(",") if t.tags else [],
            "created_at": t.created_at.isoformat() + "Z" if t.created_at else None,
            "completed_at": t.completed_at.isoformat() + "Z" if t.completed_at else None,
        }
        for t in tasks
    ]


@app.patch("/api/tasks/{task_id}")
async def api_update_task(task_id: int, request: Request):
    await init_db()
    body = await request.json()
    async with async_session() as session:
        stmt = select(Task).where(Task.id == task_id)
        result = await session.execute(stmt)
        task = result.scalar_one_or_none()
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        if "status" in body:
            task.status = body["status"]
            if body["status"] == "done":
                task.completed_at = datetime.utcnow()
            else:
                task.completed_at = None
        await session.commit()
    return {"ok": True}


@app.delete("/api/tasks/{task_id}")
async def api_delete_task(task_id: int):
    await init_db()
    async with async_session() as session:
        result = await session.execute(
            delete(Task).where(Task.id == task_id)
        )
        await session.commit()
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"ok": True}


@app.delete("/api/tasks")
async def api_delete_all_tasks(group_id: str = Query(...)):
    await init_db()
    async with async_session() as session:
        result = await session.execute(
            delete(Task).where(Task.group_id == group_id)
        )
        await session.commit()
    return {"ok": True, "deleted": result.rowcount}


# ---------------------------------------------------------------------------
# Web UI – Task board
# ---------------------------------------------------------------------------
@app.get("/t", response_class=RedirectResponse)
async def tasks_short(g: str = Query(...)):
    return RedirectResponse(f"/tasks/view?group_id={g}")


@app.get("/tasks/view", response_class=HTMLResponse)
async def tasks_page(group_id: str = Query(...)):
    await init_db()
    return _get_html("tasks_page.html")


# ---------------------------------------------------------------------------
# REST API – Token usage monitoring
# ---------------------------------------------------------------------------
@app.get("/api/usage")
async def api_token_usage(days: int = Query(default=30, le=90)):
    await init_db()
    async with async_session() as session:
        stmt = (
            select(
                cast(TokenUsage.created_at, Date).label("date"),
                TokenUsage.provider,
                TokenUsage.model,
                func.sum(TokenUsage.input_tokens).label("input_tokens"),
                func.sum(TokenUsage.output_tokens).label("output_tokens"),
                func.count().label("calls"),
            )
            .group_by("date", TokenUsage.provider, TokenUsage.model)
            .order_by(cast(TokenUsage.created_at, Date).desc())
            .limit(days * 5)  # generous limit for multiple models per day
        )
        result = await session.execute(stmt)
        rows = result.all()
    return [
        {
            "date": str(r.date),
            "provider": r.provider,
            "model": r.model,
            "input_tokens": r.input_tokens,
            "output_tokens": r.output_tokens,
            "calls": r.calls,
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Web UI – Token usage dashboard
# ---------------------------------------------------------------------------
@app.get("/usage", response_class=HTMLResponse)
async def usage_page():
    await init_db()
    return _get_html("usage_page.html")


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    return {"status": "ok"}
