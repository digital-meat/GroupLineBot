import os

from dotenv import load_dotenv

load_dotenv()

LINE_CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]

# LLM provider: "gemini" (default) or "claude"
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "gemini").lower()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

DATABASE_URL = os.environ["DATABASE_URL"]  # e.g. postgresql+asyncpg://user:pass@host/db
SUMMARY_MESSAGE_LIMIT = int(os.getenv("SUMMARY_MESSAGE_LIMIT", "200"))
APP_URL = os.getenv("APP_URL", "")  # e.g. https://your-app.vercel.app
