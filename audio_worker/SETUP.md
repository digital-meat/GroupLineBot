# 練習レビュー セットアップガイド

## アーキテクチャ

```
Google Drive (WAV原本 + 分割済みMP3)
       ↕
Google Colab (音声処理: 分割 + ゲイン正規化 + MP3変換)
       ↕ REST API
Vercel + Neon DB (Web UI + メタデータ)
```

音声データは一切Vercelを通らない。Neon DBにはメタデータ（タイムスタンプ、アノテーション等）のみ。

---

## 1. Vercel側の設定（すでにデプロイ済みの場合）

新しいテーブルは初回アクセス時に自動作成されるので、**特に何もしなくてOK**。

デプロイ後、`https://your-app.vercel.app/health` にアクセスしてDBの初期化を走らせる。

### Web UIのURL

```
https://your-app.vercel.app/practice/view?group_id=YOUR_GROUP_ID
```

ショートカット:
```
https://your-app.vercel.app/p?g=YOUR_GROUP_ID
```

---

## 2. Google OAuth Client ID の取得（Web UIでDrive音声再生するため）

Web UIからGoogle Driveの音声を直接再生するために、OAuth Client IDが必要。

### 手順

1. [Google Cloud Console](https://console.cloud.google.com/) を開く
2. プロジェクトを選択（なければ新規作成）
3. 「APIとサービス」→「認証情報」
4. 「認証情報を作成」→「OAuth クライアント ID」
5. アプリケーションの種類: **ウェブ アプリケーション**
6. 「承認済みの JavaScript 生成元」に追加:
   - `https://your-app.vercel.app`
   - `http://localhost:8000` (ローカル開発用)
7. 作成して **クライアントID** をコピー

### Web UIでの設定

初回アクセス時にプロンプトでClient IDを聞かれるので入力する。
ブラウザのlocalStorageに保存されるので、以降は聞かれない。

### Drive APIの有効化

同じGCPプロジェクトで:
1. 「APIとサービス」→「ライブラリ」
2. 「Google Drive API」を検索して有効化

---

## 3. Google Colabでの処理

### 初回セットアップ

1. `audio_worker/practice_review.ipynb` をColabで開く
2. 設定セルの以下を変更:
   - `APP_URL`: Vercelのデプロイ先URL
   - `GROUP_ID`: LINEのグループID
   - `DRIVE_INPUT_DIR`: 練習録音のWAVがあるDriveフォルダのパス
   - `DRIVE_OUTPUT_DIR`: 分割後のファイルを保存するフォルダ

### 処理フロー

```
[設定] → [Driveマウント] → [処理エンジン読み込み] → [Drive API認証] → [API接続確認] → [メイン処理]
```

上から順に全セル実行でOK。

### メイン処理の中身

1. 各WAVファイルに対して:
   - ゲイン正規化（マイクゲインが低い問題を自動補正）
   - 全体のMP3を作成（Web再生用）
   - 音量エンベロープを解析 → 低音量区間で曲の切れ目を検出
   - 各セグメントを切り出し + 正規化 + MP3変換
   - Google Drive File IDを取得
   - Vercel APIにセッション情報 + セグメント情報を登録

### パラメータの調整

| パラメータ | デフォルト | 説明 |
|---|---|---|
| `MEAN_DROP_DB` | 25.0 | 全体平均からこのdB以上下がったら境界。曲間が短い場合は下げる |
| `MIN_LOW_SEC` | 4.0 | 低音量がこの秒数以上続いたら境界。MCが入る場合は上げる |
| `MIN_TRACK_SEC` | 60.0 | これより短いセグメントは隣にマージ。短い曲がある場合は下げる |
| `TARGET_PEAK_DB` | -1.0 | ノーマライズ目標ピーク |

### 切り抜きリクエスト

Web UIから「切り抜きリクエスト」を送信すると、Colabの最後のセルで処理できる。

---

## 4. 使い方（日常の流れ）

### 練習後

1. 録音WAVをGoogle Driveの入力フォルダにアップロード
2. Colabノートブックを開いて全セル実行
3. 完了したらWeb UIで確認

### レビュー

1. Web UI (`/practice/view?group_id=...`) を開く
2. Googleでログイン（Drive音声再生のため）
3. セッションを選択
4. 波形表示 + 5秒スキップで自在に聴ける
5. 良いポイントにアノテーション（タイムスタンプ付きコメント）
6. セグメントに曲名をつける
7. 特定区間を切り抜きたい場合は切り抜きリクエスト

### キーボードショートカット

| キー | アクション |
|---|---|
| Space | 再生/一時停止 |
| ← | 5秒戻る |
| → | 5秒進む |
| Shift+← | 30秒戻る |
| Shift+→ | 30秒進む |

---

## フォルダ構成例

```
Google Drive/
  _DIGITALMEAT_REC/
    02_練習録音・録画/          ← WAV原本
      2026-03-10_スタジオ練習.wav
      2026-03-17_スタジオ練習.wav
    03_分割済み/                ← 処理後の出力
      2026-03-10_スタジオ練習/
        2026-03-10_スタジオ練習.mp3        ← 全体MP3
        2026-03-10_スタジオ練習_track01.wav
        2026-03-10_スタジオ練習_track01.mp3
        2026-03-10_スタジオ練習_track02.wav
        2026-03-10_スタジオ練習_track02.mp3
        ...
      _clips/                  ← 切り抜き
        イントロかっこいい.mp3
```
