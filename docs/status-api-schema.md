# Public Status API schema

Minecraft、Herta、将来のWeb・APIサービスを同じ形式で公開するステータスAPIです。

## Endpoint

```text
GET /api/status.json
```

サービスのLive Statusに加えて、Status Centerで明示的に公開されたIncident / Maintenance / Announcementを返します。

公開CMSデータは `console.ivrm.jp/api/public/status-feed` から取得します。OCI Status APIへSupabase Service Roleなどの管理Credentialは配置しません。

## Response example

```json
{
  "generated_at": "2026-08-30T03:30:00+00:00",
  "overall_status": "operational",
  "services": [
    {
      "id": "minecraft-network",
      "group": "ゲームサービス",
      "name": "Minecraft Network",
      "description": "Minecraft Server",
      "status": "operational",
      "checked_at": "2026-08-30T03:29:30+00:00",
      "last_received_at": "2026-08-30T03:29:30+00:00",
      "timeline": ["unknown", "operational"],
      "timeline_details": [
        {
          "start_at": "2026-08-30T01:30:00+00:00",
          "end_at": "2026-08-30T02:30:00+00:00",
          "status": "operational",
          "related_incident_ids": [],
          "summary": null
        },
        {
          "start_at": "2026-08-30T02:30:00+00:00",
          "end_at": "2026-08-30T03:30:00+00:00",
          "status": "operational",
          "related_incident_ids": ["INC-ABCDEF123456"],
          "summary": "接続障害の原因を特定しました"
        }
      ],
      "meta": {
        "type": "minecraft",
        "connection": "mc.ivrm.jp",
        "playersOnline": 0,
        "playersMax": 10,
        "mode": "Minecraft Server"
      }
    }
  ],
  "incidents": [
    {
      "public_id": "INC-ABCDEF123456",
      "title": "Minecraft Network 接続障害",
      "status": "identified",
      "impact": "major",
      "affected_service_ids": ["minecraft-network"],
      "started_at": "2026-08-30T03:10:00+00:00",
      "resolved_at": null,
      "updated_at": "2026-08-30T03:25:00+00:00",
      "summary": "接続障害の原因を特定しました",
      "source": "manual",
      "updates": [
        {
          "status": "investigating",
          "message": "接続障害を調査しています",
          "published_at": "2026-08-30T03:10:00+00:00"
        },
        {
          "status": "identified",
          "message": "接続障害の原因を特定しました",
          "published_at": "2026-08-30T03:25:00+00:00"
        }
      ]
    }
  ],
  "maintenance": [],
  "announcements": [],
  "content_meta": {
    "source": "live",
    "generated_at": "2026-08-30T03:29:58+00:00",
    "fetched_at": "2026-08-30T03:30:00+00:00",
    "stale": false
  }
}
```

## Status values

```text
operational
maintenance
degraded
outage
unknown
```

全体状態と時間帯の状態は、次の優先順位で最も悪い値を採用します。

```text
operational < maintenance < degraded < outage < unknown
```

公開Incidentは説明・履歴のSourceであり、Incident CMSの存在だけで`overall_status`を上書きしません。`overall_status`は引き続きLive Service Statusから算出します。

## Timeline

`timeline`は24時間を1時間単位で古い順に返します。同じ時間帯に複数の記録がある場合は最も悪い状態を採用し、データがない時間帯は`unknown`です。

`timeline_details`は既存`timeline`と同じbucket順で、次を追加します。

- `start_at` / `end_at`
- `status`
- `related_incident_ids`
- `summary`

Incidentの時間範囲とAffected Service IDがbucketへ重なる場合だけ関連付けます。紐づく公開Incidentがない場合は原因を推測せず、`related_incident_ids=[]`、`summary=null`を返します。

## Incident lifecycle

```text
investigating
identified
monitoring
resolved
```

Impact:

```text
none
minor
major
critical
```

Incident Updateは公開された追記履歴です。過去Updateを上書きして経緯を消さない契約です。

## Maintenance

公開Maintenanceは次の状態を返します。

```text
scheduled
in_progress
completed
cancelled
```

Status Portalの公開NoticeとReliability SLOのMaintenance Windowは別責務です。管理側では関連付けできますが、Public APIへ内部Reliability Window IDは公開しません。

## Announcement

Announcementは`info`または`warning`で、公開時刻・期限・Affected Service IDを持ちます。初期版本文はplain textです。

## Public Content cache

公開CMS FeedはLive Minecraft/Herta監視から独立して扱います。

1. `https://console.ivrm.jp/api/public/status-feed`を短いtimeoutで取得
2. JSON schemaを厳格検証
3. 正常Feedを`/data/public-status-content.json`へatomic replaceで保存
4. 上流取得失敗時はlast-known-good cacheを使用
5. cacheも利用不能ならCMS領域だけ空配列にし、Live Status APIは継続

`content_meta.source`:

```text
live
cache
none
```

`stale=true`はCMS Feedの鮮度不足を示します。古いCMS cacheを新しい障害として自動生成することはありません。

## Stale

- Herta: 最終受信から120秒を超えると`unknown`
- Minecraft: collectorの更新時刻が既定300秒を超えると`unknown`
- Public CMS content: 既定600秒を超えると`content_meta.stale=true`

stale判定は公開APIを呼び出すたびに評価します。

## 公開しない情報

- Hertaの内部`checks`
- PostgreSQL・Redis・Workerの個別状態
- AWS / OCIの内部管理情報
- 内部IP、DB接続情報、環境変数
- Supabase Service Role / Secret
- Webhook URL / Token
- Status CMSの内部UUID / `source_ref`
- Audit actor / Audit metadata
- HMAC署名、共有鍵、request ID
- raw log、スタックトレース、内部エラー全文
