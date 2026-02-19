# GroupLineBot

バンドのグループLINEチャットを要約してくれるBotです。LLM（Claude）を使ってチャットログを要約し、タスクを自動抽出します。

## 機能

- **メッセージ保存**: グループ内の全テキストメッセージをDBに保存
- **チャット要約**: `要約して` or `/summary` で直近のチャットをLLMが要約
- **タスク抽出**: 要約時にチャットからTODO/タスクを自動抽出して別テーブルに保存
- **タスク管理**: `/tasks` で未完了タスク一覧、`/done [番号]` で完了マーク

## コマンド

| コマンド | 説明 |
|---------|------|
| `要約して` `/summary` `/まとめ` | 直近のチャットを要約 |
| `タスク一覧` `/tasks` `/タスク` | 未完了タスクを表示 |
| `/done [番号]` | タスクを完了にする |

## セットアップ

### 1. LINE公式アカウント作成

1. [LINE Developers Console](https://developers.line.biz/) でプロバイダーとチャネルを作成
2. Messaging API チャネルを作成
3. チャネルシークレットとチャネルアクセストークンを取得

### 2. 環境変数設定

```bash
cp .env.example .env
# .env を編集して各種キーを設定
```

### 3. インストール & 起動

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 4. Webhook設定

LINE Developers ConsoleでWebhook URLを設定:
```
https://your-domain.com/callback
```

ngrokでローカル開発する場合:
```bash
ngrok http 8000
```

## 技術スタック

- **Python** + **FastAPI**
- **SQLAlchemy** (async) + **SQLite**
- **LINE Messaging API** (SDK v3)
- **Anthropic Claude API** (要約 & タスク抽出)
