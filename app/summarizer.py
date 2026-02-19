"""LLM-powered chat summarizer using Claude API."""

import json

import anthropic

from app.config import ANTHROPIC_API_KEY

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

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


def build_chat_log(messages: list[dict]) -> str:
    """Format DB messages into a readable chat log for the LLM."""
    lines: list[str] = []
    for m in messages:
        ts = m["timestamp"].strftime("%m/%d %H:%M")
        name = m["display_name"]
        content = m["content"] or f"[{m['message_type']}]"
        lines.append(f"[{ts}] {name}: {content}")
    return "\n".join(lines)


def summarize_chat(messages: list[dict]) -> dict:
    """Send chat log to Claude and get back summary + tasks.

    Returns:
        {"summary": str, "tasks": [{"title": str, "assignee": str|None}]}
    """
    chat_log = build_chat_log(messages)

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=2048,
        system=SUMMARY_SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": f"以下のチャットログを要約してください:\n\n{chat_log}",
            }
        ],
    )

    raw = response.content[0].text.strip()
    # Strip markdown code fences if present
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1]
        if raw.endswith("```"):
            raw = raw[: raw.rfind("```")]
        raw = raw.strip()

    return json.loads(raw)
