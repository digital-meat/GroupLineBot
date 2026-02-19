"""LLM-powered chat summarizer with pluggable provider (Gemini / Claude)."""

import json
import logging
import os

from app.config import ANTHROPIC_API_KEY, GEMINI_API_KEY, LLM_PROVIDER

logger = logging.getLogger(__name__)

SUMMARY_SYSTEM_PROMPT = """\
あなたはバンドのグループLINEチャットの要約アシスタントです。

以下のチャットログを読んで、2つのことをしてください:

1. **キャッチアップ要約**: チャットに追いつけていない人向けの要約。
   - 重要な決定事項
   - 話題になったトピック
   - 次のアクション
   を箇条書きで簡潔にまとめてください。

2. **タスク抽出**: チャットの中で出てきた「やること」「TODO」「頼まれたこと」を抽出してください。
   誰がやるか分かれば assignee に入れてください。

出力は必ず以下のJSON形式で返してください（他のテキストは含めないで）:
{
  "summary": "キャッチアップ要約のテキスト（markdown箇条書き可）",
  "tasks": [
    {"title": "タスクの内容", "assignee": "担当者名 or null"}
  ]
}
"""

CHUNK_SUMMARY_PROMPT = """\
以下はグループLINEチャットの一部です。
重要な情報を漏らさず、簡潔に要約してください（箇条書き）。
決定事項・TODO・重要な話題を必ず含めてください。
プレーンテキストで返してください（JSON不要）。
"""

USER_PROMPT_TEMPLATE = "以下のチャットログを要約してください:\n\n{chat_log}"

MAX_CHAT_LOG_CHARS = int(os.getenv("MAX_CHAT_LOG_CHARS", "60000"))


def _format_lines(messages: list[dict]) -> list[str]:
    """Format DB messages into individual log lines."""
    lines: list[str] = []
    for m in messages:
        ts = m["timestamp"].strftime("%m/%d %H:%M")
        name = m["display_name"]
        content = m["content"] or f"[{m['message_type']}]"
        lines.append(f"[{ts}] {name}: {content}")
    return lines


def build_chat_log(messages: list[dict]) -> str:
    """Format DB messages into a readable chat log for the LLM."""
    return "\n".join(_format_lines(messages))


def _split_lines_into_chunks(lines: list[str], max_chars: int) -> list[list[str]]:
    """Split lines into chunks that each fit within max_chars."""
    chunks: list[list[str]] = []
    current: list[str] = []
    current_len = 0
    for line in lines:
        line_len = len(line) + 1  # +1 for newline
        if current and current_len + line_len > max_chars:
            chunks.append(current)
            current = []
            current_len = 0
        current.append(line)
        current_len += line_len
    if current:
        chunks.append(current)
    return chunks


def _strip_code_fences(raw: str) -> str:
    """Strip markdown code fences if present."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1]
        if raw.endswith("```"):
            raw = raw[: raw.rfind("```")]
        raw = raw.strip()
    return raw


# ---------------------------------------------------------------------------
# Gemini provider
# ---------------------------------------------------------------------------

def _call_gemini(prompt: str, system: str = SUMMARY_SYSTEM_PROMPT) -> str:
    import google.generativeai as genai

    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel(
        model_name="gemini-2.5-flash",
        system_instruction=system,
    )
    response = model.generate_content(
        prompt,
        generation_config=genai.types.GenerationConfig(max_output_tokens=2048),
    )
    return response.text


# ---------------------------------------------------------------------------
# Claude provider
# ---------------------------------------------------------------------------

def _call_claude(prompt: str, system: str = SUMMARY_SYSTEM_PROMPT) -> str:
    import anthropic

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=2048,
        system=system,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_PROVIDERS = {
    "gemini": _call_gemini,
    "claude": _call_claude,
}


def summarize_chat(messages: list[dict]) -> dict:
    """Send chat log to LLM and get back summary + tasks.

    If the chat log is too long for a single call, older messages are
    summarized in chunks first, and the condensed context is prepended
    to the most recent messages for the final summary.

    Returns:
        {"summary": str, "tasks": [{"title": str, "assignee": str|None}]}
    """
    provider_fn = _PROVIDERS.get(LLM_PROVIDER)
    if provider_fn is None:
        raise ValueError(
            f"Unknown LLM_PROVIDER: {LLM_PROVIDER!r}. Choose 'gemini' or 'claude'."
        )

    lines = _format_lines(messages)
    full_log = "\n".join(lines)

    # Fast path: fits in a single call
    if len(full_log) <= MAX_CHAT_LOG_CHARS:
        raw = provider_fn(USER_PROMPT_TEMPLATE.format(chat_log=full_log))
        return json.loads(_strip_code_fences(raw))

    # Slow path: chunked summarization
    # Reserve half the budget for the most recent messages
    recent_budget = MAX_CHAT_LOG_CHARS // 2
    chunk_budget = MAX_CHAT_LOG_CHARS // 2

    # Split lines: recent (kept verbatim) vs older (to be condensed)
    recent_lines: list[str] = []
    recent_len = 0
    split_idx = len(lines)
    for i in range(len(lines) - 1, -1, -1):
        line_len = len(lines[i]) + 1
        if recent_len + line_len > recent_budget:
            break
        recent_lines.append(lines[i])
        recent_len += line_len
        split_idx = i
    recent_lines.reverse()

    older_lines = lines[:split_idx]

    # Summarize older chunks
    chunk_summaries: list[str] = []
    if older_lines:
        chunks = _split_lines_into_chunks(older_lines, chunk_budget)
        logger.info(
            "Chat log too long (%d chars). Summarizing %d older chunk(s) first.",
            len(full_log), len(chunks),
        )
        for idx, chunk in enumerate(chunks):
            chunk_text = "\n".join(chunk)
            prompt = f"以下のチャットログを要約してください:\n\n{chunk_text}"
            summary = provider_fn(prompt, system=CHUNK_SUMMARY_PROMPT)
            chunk_summaries.append(summary.strip())
            logger.info("Chunk %d/%d summarized.", idx + 1, len(chunks))

    # Build final prompt with condensed older context + recent verbatim
    context_block = "\n\n".join(chunk_summaries)
    recent_block = "\n".join(recent_lines)

    final_chat_log = (
        f"【過去の会話の要約】\n{context_block}\n\n"
        f"【直近の会話（原文）】\n{recent_block}"
    )

    raw = provider_fn(USER_PROMPT_TEMPLATE.format(chat_log=final_chat_log))
    return json.loads(_strip_code_fences(raw))
