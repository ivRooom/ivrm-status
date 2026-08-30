# ivRooom Status API

FastAPI・SQLiteで構成する、`status.ivrm.jp`のステータス受信・統合APIです。

## API

- `POST /api/internal/status-ingest`: HMAC署名付き内部受信
- `GET /api/status.json`: Minecraft / Herta Live Status + 公開Incident / Maintenance / Announcement
- `GET /api/status-history.json`: 最大30日の稼働履歴 + 同期間の公開CMS content
- `GET /healthz`: APIとSQLiteの疎通確認

## Public CMS Feed

公開Incident / Maintenance / Announcementの編集Source of Truthは`console.ivrm.jp` / Supabase `ivrm-core`です。

Status APIは次のsanitized endpointだけを読み取ります。

```text
https://console.ivrm.jp/api/public/status-feed
```

OCIへSupabase Service Role、Webhook URL、管理Tokenを配置しません。

取得した正常Feedは既定で次へlast-known-good cacheします。

```text
/data/public-status-content.json
```

上流Feedが一時的に失敗しても、Minecraft / Herta Live Statusは継続し、CMS領域だけcacheへfallbackします。cacheも利用不能な場合はCMS配列を空にしてLive Statusを返します。

主な設定:

```text
STATUS_PUBLIC_CONTENT_FEED_URL=https://console.ivrm.jp/api/public/status-feed
STATUS_PUBLIC_CONTENT_CACHE_PATH=/data/public-status-content.json
STATUS_PUBLIC_CONTENT_TIMEOUT_SECONDS=1.5
STATUS_PUBLIC_CONTENT_REFRESH_SECONDS=30
STATUS_PUBLIC_CONTENT_STALE_SECONDS=600
```

## ローカル実行

```bash
cd services/status-api
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
export STATUS_DB_PATH=/tmp/ivrm-status.db
export HERTA_INGEST_SECRET=local-development-secret-32-characters
export MINECRAFT_CURRENT_PATH=/tmp/current.json
export MINECRAFT_HISTORY_PATH=/tmp/history.json
# 外部Public Feedを使わずLive Statusだけ確認する場合
export STATUS_PUBLIC_CONTENT_FEED_URL=
uvicorn app.main:create_app --factory --reload --port 8080
```

アプリケーションファクトリを使用するため、モジュールimport時にはSQLiteや`/data`へアクセスしません。本番コンテナではComposeから`STATUS_DB_PATH=/data/status.db`を渡します。

## テスト

```bash
pytest
```

テストではPublic CMS Feedを無効化またはFetch関数を注入し、外部ネットワークに依存しません。

## 設計上の境界

Hertaの内部`checks`は受信せず、公開に必要なservice metadata、status、checked_at、version、summaryだけを保存します。

公開CMS Feedは明示的にpublishされた文章だけを受け入れます。Incidentの存在だけで`overall_status`を上書きせず、障害理由はAffected Service + 時間範囲が一致する`timeline_details`へ関連情報として付与します。公開Incidentがなければ原因を推測しません。

公開APIへ含めないもの:

- DB / Redis / AWS / OCIの内部管理情報
- Discord Guildの非公開情報
- 内部IP / raw log / stack trace
- Supabase Service Role / Webhook URL / Token
- CMS内部UUID / `source_ref` / Audit actor
