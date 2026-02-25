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
   ※「前回の要約」が提供されている場合、その内容も含めてタスクを抽出してください。
   ただし要約は新しいメッセージのみを対象にしてください。

3. **タグ付け**: 各タスクに1-3個の短いタグを付けてください。
   タスクの内容を分類する短いラベルです。
   例: 買い物, 連絡, 練習, 準備, 確認, 予約, 作業, 会計, 検討, 相談

出力は必ず以下のJSON形式で返してください（他のテキストは含めないで）:
{
  "summary": "キャッチアップ要約のテキスト（markdown箇条書き可）",
  "tasks": [
    {"title": "タスクの内容", "assignee": "担当者名 or null", "tags": ["タグ1", "タグ2"]}
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

USER_PROMPT_WITH_CONTEXT_TEMPLATE = """\
【前回の要約（タスク抽出の参考にしてください）】
{previous_summary}

【新しいチャットログ（要約対象）】
{chat_log}"""

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


def _parse_llm_json(raw: str) -> dict:
    """Best-effort JSON parse from LLM output.

    LLMs sometimes return truncated or malformed JSON.  We try
    progressively more lenient strategies before giving up.
    """
    cleaned = _strip_code_fences(raw)

    # 1. Direct parse
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # 2. Extract the first { ... } block (greedy)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError:
            pass

    # 3. Truncated JSON – try closing open strings / arrays / objects
    if start != -1:
        fragment = cleaned[start:]
        # Close unterminated string
        if fragment.count('"') % 2 == 1:
            fragment += '"'
        # Close open arrays / objects
        for ch in ("]", "}"):
            while fragment.count(ch) < fragment.count("{" if ch == "}" else "["):
                fragment += ch
        try:
            result = json.loads(fragment)
            logger.warning("LLM output was truncated – repaired JSON but summary may be incomplete")
            if isinstance(result.get("summary"), str):
                result["summary"] += "\n\n⚠ 要約が途中で切れた可能性があります"
            return result
        except json.JSONDecodeError:
            pass

    # 4. Give up – return just the summary text so the bot still replies
    logger.warning("Could not parse LLM JSON, returning raw text as summary")
    return {"summary": raw.strip(), "tasks": []}


# ---------------------------------------------------------------------------
# Gemini provider
# ---------------------------------------------------------------------------

_SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "assignee": {"type": "string", "nullable": True},
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": ["title"],
            },
        },
    },
    "required": ["summary", "tasks"],
}


def _call_gemini(prompt: str, system: str = SUMMARY_SYSTEM_PROMPT, *, json_mode: bool = False) -> tuple[str, dict]:
    import google.generativeai as genai

    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel(
        model_name="gemini-2.5-flash",
        system_instruction=system,
    )
    gen_config = {"max_output_tokens": 8192}
    if json_mode:
        gen_config["response_mime_type"] = "application/json"
        gen_config["response_schema"] = _SUMMARY_SCHEMA
    response = model.generate_content(
        prompt,
        generation_config=genai.types.GenerationConfig(**gen_config),
    )
    usage = {}
    if hasattr(response, "usage_metadata") and response.usage_metadata:
        meta = response.usage_metadata
        usage = {
            "provider": "gemini",
            "model": "gemini-2.5-flash",
            "input_tokens": getattr(meta, "prompt_token_count", 0) or 0,
            "output_tokens": getattr(meta, "candidates_token_count", 0) or 0,
        }
    return response.text, usage


# ---------------------------------------------------------------------------
# Claude provider
# ---------------------------------------------------------------------------

def _call_claude(prompt: str, system: str = SUMMARY_SYSTEM_PROMPT, *, json_mode: bool = False) -> tuple[str, dict]:
    import anthropic

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=8192,
        system=system,
        messages=[{"role": "user", "content": prompt}],
    )
    usage = {}
    if response.usage:
        usage = {
            "provider": "claude",
            "model": response.model,
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        }
    return response.content[0].text, usage


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_PROVIDERS = {
    "gemini": _call_gemini,
    "claude": _call_claude,
}


def summarize_chat(
    messages: list[dict], previous_summary: str | None = None
) -> dict:
    """Send chat log to LLM and get back summary + tasks.

    If *previous_summary* is given, it is prepended so the LLM can
    extract tasks from broader context without re-summarizing old messages.

    If the chat log is too long for a single call, older messages are
    summarized in chunks first, and the condensed context is prepended
    to the most recent messages for the final summary.

    Returns:
        {"summary": str, "tasks": [...], "token_usage": [usage_dict, ...]}
    """
    provider_fn = _PROVIDERS.get(LLM_PROVIDER)
    if provider_fn is None:
        raise ValueError(
            f"Unknown LLM_PROVIDER: {LLM_PROVIDER!r}. Choose 'gemini' or 'claude'."
        )

    all_usage: list[dict] = []

    lines = _format_lines(messages)
    full_log = "\n".join(lines)

    def _build_prompt(chat_log: str) -> str:
        if previous_summary:
            return USER_PROMPT_WITH_CONTEXT_TEMPLATE.format(
                previous_summary=previous_summary, chat_log=chat_log
            )
        return USER_PROMPT_TEMPLATE.format(chat_log=chat_log)

    # Fast path: fits in a single call
    if len(full_log) <= MAX_CHAT_LOG_CHARS:
        raw, usage = provider_fn(_build_prompt(full_log), json_mode=True)
        if usage:
            usage["purpose"] = "summary"
            all_usage.append(usage)
        parsed = _parse_llm_json(raw)
        parsed["token_usage"] = all_usage
        return parsed

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
            summary_text, usage = provider_fn(prompt, system=CHUNK_SUMMARY_PROMPT)
            if usage:
                usage["purpose"] = "chunk_summary"
                all_usage.append(usage)
            chunk_summaries.append(summary_text.strip())
            logger.info("Chunk %d/%d summarized.", idx + 1, len(chunks))

    # Build final prompt with condensed older context + recent verbatim
    context_block = "\n\n".join(chunk_summaries)
    recent_block = "\n".join(recent_lines)

    final_chat_log = (
        f"【過去の会話の要約】\n{context_block}\n\n"
        f"【直近の会話（原文）】\n{recent_block}"
    )

    raw, usage = provider_fn(_build_prompt(final_chat_log), json_mode=True)
    if usage:
        usage["purpose"] = "summary"
        all_usage.append(usage)
    parsed = _parse_llm_json(raw)
    parsed["token_usage"] = all_usage
    return parsed
