"""LLM-powered chat summarizer with pluggable provider (Gemini / Claude)."""

import json
import os

from app.config import ANTHROPIC_API_KEY, GEMINI_API_KEY, LLM_PROVIDER

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

USER_PROMPT_TEMPLATE = "以下のチャットログを要約してください:\n\n{chat_log}"


MAX_CHAT_LOG_CHARS = int(os.getenv("MAX_CHAT_LOG_CHARS", "60000"))


def build_chat_log(messages: list[dict]) -> str:
    """Format DB messages into a readable chat log for the LLM.

    If the total log exceeds MAX_CHAT_LOG_CHARS, older messages are
    dropped so that the most recent conversation is always included.
    """
    lines: list[str] = []
    for m in messages:
        ts = m["timestamp"].strftime("%m/%d %H:%M")
        name = m["display_name"]
        content = m["content"] or f"[{m['message_type']}]"
        lines.append(f"[{ts}] {name}: {content}")

    full = "\n".join(lines)
    if len(full) <= MAX_CHAT_LOG_CHARS:
        return full

    # Keep the most recent messages that fit within the limit
    truncated: list[str] = []
    total = 0
    for line in reversed(lines):
        # +1 for the newline separator
        if total + len(line) + 1 > MAX_CHAT_LOG_CHARS:
            break
        truncated.append(line)
        total += len(line) + 1
    truncated.reverse()
    return f"（※ 古いメッセージは省略されています）\n" + "\n".join(truncated)


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

def _call_gemini(chat_log: str) -> str:
    import google.generativeai as genai

    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel(
        model_name="gemini-2.5-flash",
        system_instruction=SUMMARY_SYSTEM_PROMPT,
    )
    response = model.generate_content(
        USER_PROMPT_TEMPLATE.format(chat_log=chat_log),
        generation_config=genai.types.GenerationConfig(max_output_tokens=2048),
    )
    return response.text


# ---------------------------------------------------------------------------
# Claude provider
# ---------------------------------------------------------------------------

def _call_claude(chat_log: str) -> str:
    import anthropic

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=2048,
        system=SUMMARY_SYSTEM_PROMPT,
        messages=[
            {"role": "user", "content": USER_PROMPT_TEMPLATE.format(chat_log=chat_log)}
        ],
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

    Provider is selected by the LLM_PROVIDER env var ("gemini" or "claude").

    Returns:
        {"summary": str, "tasks": [{"title": str, "assignee": str|None}]}
    """
    provider_fn = _PROVIDERS.get(LLM_PROVIDER)
    if provider_fn is None:
        raise ValueError(
            f"Unknown LLM_PROVIDER: {LLM_PROVIDER!r}. Choose 'gemini' or 'claude'."
        )

    chat_log = build_chat_log(messages)
    raw = provider_fn(chat_log)
    return json.loads(_strip_code_fences(raw))
