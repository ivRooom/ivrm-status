# Status Ingest HMAC認証仕様

## エンドポイント

```text
POST /api/internal/status-ingest
```

Hertaなど各サービスは、サービスごとに異なる共有秘密鍵でHMAC-SHA256署名を作成します。

## 必須ヘッダー

```text
Content-Type: application/json
X-IVRM-Service-Id: herta-discord-bot
X-IVRM-Timestamp: Unix秒
X-IVRM-Request-Id: UUID
X-IVRM-Body-SHA256: 本文SHA-256 hex
X-IVRM-Signature: v1=HMAC-SHA256 hex
```

## canonical string

```text
POST
/api/internal/status-ingest
{timestamp}
{request_id}
{service_id}
{body_sha256}
```

末尾改行は付けません。JSON本文は送信に使用するバイト列そのものをSHA-256へ入力してください。

## 検証順序

1. Content-Typeと本文サイズ
2. 必須ヘッダー形式
3. service IDの許可リスト
4. timestampの許容差
5. 本文SHA-256
6. constant-time HMAC比較
7. rate limit
8. JSON Schema
9. service ID整合性
10. SQLite上のrequest ID再利用検知

認証失敗の外部レスポンスは理由を細分化せず、ログにも秘密鍵・署名・本文全文を残しません。

## 既定値

| 設定 | 値 |
|---|---:|
| 最大本文 | 16 KiB |
| 時計ずれ | ±120秒 |
| request ID保持 | 600秒 |
| rate limit | 10回/分/サービス |
| Herta stale | 120秒 |

失敗時の実装上のエラー分類（`services/status-api/app/main.py`）: 非JSON `415`、本文過大 `413`、ヘッダー不正/認証失敗 `401`、rate limit `429`、schema不正/ID不一致 `400`、同じrequest IDの再利用 `409`、SQLite保存失敗 `500`。正常受信は `202`（accepted=true）。パケット単位の再送/誤署名/clock skewの動作は実行試験とGitHub Actions記録で確認し、文書自体を実行済みの証拠としない。
