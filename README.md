# GroupLineBot

バンドのグループLINEチャットを要約してくれるBotです。LLM（Claude）を使ってチャットログを要約し、タスクを自動抽出します。

## 機能

- **メッセージ保存**: グループ内の全テキストメッセージをDBに保存（画像・動画・スタンプは無視）
- **チャット要約**: `要約して` or `/summary` で直近のチャットをLLMが要約
- **タスク抽出**: 要約時にチャットからTODO/タスクを自動抽出して別テーブルに保存
- **タスク管理**: `/tasks` で未完了タスク一覧、`/done [番号]` で完了マーク

## コマンド

| コマンド | 説明 |
|---------|------|
| `要約して` `/summary` `/まとめ` | 直近のチャットを要約 |
| `タスク一覧` `/tasks` `/タスク` | 未完了タスクを表示 |
| `/done [番号]` | タスクを完了にする |

## デプロイ (Vercel)

### 1. LINE公式アカウント作成

1. [LINE Developers Console](https://developers.line.biz/) でプロバイダーとチャネルを作成
2. Messaging API チャネルを作成
3. チャネルシークレットとチャネルアクセストークンを取得

### 2. Vercel Postgres セットアップ

1. Vercel ダッシュボードで Storage → Create Database → Postgres
2. 接続文字列を取得

### 3. デプロイ

```bash
# Vercel CLIでデプロイ
npm i -g vercel
vercel

# 環境変数を設定
vercel env add LINE_CHANNEL_SECRET
vercel env add LINE_CHANNEL_ACCESS_TOKEN
vercel env add ANTHROPIC_API_KEY
vercel env add DATABASE_URL   # postgresql+asyncpg://... 形式

# 本番デプロイ
vercel --prod
```

### 4. Webhook設定

LINE Developers ConsoleでWebhook URLを設定:
```
https://your-project.vercel.app/callback
```

### 5. DBテーブル作成

初回アクセス時（`/health` を叩く）に自動でテーブルが作成されます。

## ローカル開発

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# .env を編集

uvicorn app.main:app --host 0.0.0.0 --port 8000
```

ngrokでWebhookを公開:
```bash
ngrok http 8000
```

## 技術スタック

- **Python** + **FastAPI**
- **SQLAlchemy** (async) + **PostgreSQL** (asyncpg)
- **LINE Messaging API** (SDK v3)
- **Anthropic Claude API** (要約 & タスク抽出)
- **Vercel** (サーバーレスデプロイ)
