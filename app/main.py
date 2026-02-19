"""GroupLineBot – LINE group chat summarizer powered by LLM."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.webhooks import (
    JoinEvent,
    MessageEvent,
    TextMessageContent,
)
from linebot.v3.webhook import WebhookParser
from sqlalchemy import select

from app.config import LINE_CHANNEL_SECRET, SUMMARY_MESSAGE_LIMIT
from app.database import async_session, init_db
from app.line_client import get_api, get_display_name, reply_text
from app.models import Message, Summary, Task
from app.summarizer import summarize_chat

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

parser = WebhookParser(LINE_CHANNEL_SECRET)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    logger.info("Database initialized")
    yield


app = FastAPI(title="GroupLineBot", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Webhook endpoint
# ---------------------------------------------------------------------------
@app.post("/callback")
async def callback(
    request: Request,
    x_line_signature: str = Header(...),
):
    body = (await request.body()).decode("utf-8")

    try:
        events = parser.parse(body, x_line_signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature")

    api = get_api()

    for event in events:
        if isinstance(event, JoinEvent):
            logger.info("Bot joined group: %s", event.source.group_id)

        elif isinstance(event, MessageEvent):
            await handle_message(api, event)

    return "OK"


# ---------------------------------------------------------------------------
# Message handler
# ---------------------------------------------------------------------------
async def handle_message(api, event: MessageEvent):
    group_id = getattr(event.source, "group_id", None)
    if not group_id:
        return  # Only handle group messages

    user_id = event.source.user_id or "unknown"
    display_name = await get_display_name(api, group_id, user_id)

    # Determine message content
    msg_type = event.message.type
    content = None
    if isinstance(event.message, TextMessageContent):
        content = event.message.text

    # Store message
    async with async_session() as session:
        msg = Message(
            group_id=group_id,
            user_id=user_id,
            display_name=display_name,
            message_type=msg_type,
            content=content,
            line_message_id=event.message.id,
        )
        session.add(msg)
        await session.commit()

    # Check for bot commands
    if content:
        cmd = content.strip()
        if cmd in ("/summary", "要約して", "/まとめ"):
            await cmd_summary(api, event, group_id)
        elif cmd in ("/tasks", "タスク一覧", "/タスク"):
            await cmd_tasks(api, event, group_id)
        elif cmd.startswith("/done "):
            await cmd_done_task(api, event, group_id, cmd)


# ---------------------------------------------------------------------------
# /summary – Summarize recent chat
# ---------------------------------------------------------------------------
async def cmd_summary(api, event: MessageEvent, group_id: str):
    async with async_session() as session:
        stmt = (
            select(Message)
            .where(Message.group_id == group_id)
            .where(Message.message_type == "text")
            .where(Message.content.isnot(None))
            .order_by(Message.id.desc())
            .limit(SUMMARY_MESSAGE_LIMIT)
        )
        result = await session.execute(stmt)
        rows = result.scalars().all()

    if not rows:
        reply_text(api, event, "まだメッセージが保存されていません。")
        return

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
        result = summarize_chat(messages)
    except Exception as e:
        logger.exception("Summarization failed")
        reply_text(api, event, f"要約に失敗しました: {e}")
        return

    # Save tasks
    async with async_session() as session:
        for t in result.get("tasks", []):
            task = Task(
                group_id=group_id,
                title=t["title"],
                assignee=t.get("assignee"),
                source_summary=result["summary"][:500],
            )
            session.add(task)

        # Save summary record
        summary_record = Summary(
            group_id=group_id,
            content=result["summary"],
            message_from_id=rows[0].id,
            message_to_id=rows[-1].id,
        )
        session.add(summary_record)
        await session.commit()

    # Build response
    text = f"📝 キャッチアップ要約\n{'=' * 20}\n{result['summary']}"
    if result.get("tasks"):
        text += f"\n\n📋 抽出されたタスク\n{'=' * 20}"
        for i, t in enumerate(result["tasks"], 1):
            assignee = f" (@{t['assignee']})" if t.get("assignee") else ""
            text += f"\n{i}. {t['title']}{assignee}"
        text += "\n\n✅ タスクを完了するには: /done [番号]"

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
        reply_text(api, event, "未完了のタスクはありません 🎉")
        return

    text = "📋 未完了タスク一覧\n" + "=" * 20
    for t in tasks:
        assignee = f" (@{t.assignee})" if t.assignee else ""
        text += f"\n#{t.id} {t.title}{assignee}"
    text += "\n\n✅ 完了するには: /done [番号]"

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
        await session.commit()

    reply_text(api, event, f"✅ タスク #{task_id} を完了にしました: {task.title}")


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    return {"status": "ok"}
