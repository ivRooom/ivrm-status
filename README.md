# ivRooom Status

`status.ivrm.jp` で公開する、ivRooom公式の利用者向けサービスステータスページです。

Minecraftサーバー専用の運用画面ではなく、Web、API、Discord、ゲームなど複数サービスの稼働状況を同じ画面で公開できる構成です。

## 構成

```text
Minecraft collector
  -> current.json / history.json

Herta status-agent（次フェーズ）
  -> HTTPS POST + HMAC

OCI Status API
  -> SQLiteへHerta履歴を保存
  -> Minecraft JSONと統合
  -> GET /api/status.json

Caddy
  -> 静的UI
  -> APIパスのみstatus-apiへreverse proxy
```

## ディレクトリ

```text
index.html / assets/              公開ステータスUI
services/status-api/              FastAPI + SQLiteバックエンド
deploy/status-api/                OCI Docker Compose / Caddy設定
scripts/deploy-oci.sh             静的UIデプロイ
scripts/deploy-status-api-oci.sh  Status APIデプロイ
scripts/send-test-status.py       HMACテスト送信
docs/                             API・認証・OCI運用資料
```

## 公開API

```text
GET /api/status.json
GET /api/status-history.json?days=30
```

`/api/status-history.json` は1～30日の履歴とAvailabilityをAsia/Tokyoの日付単位で返します（デフォルト30日）。本番APIの実行コード・DB・ネットワーク境界は[DB/API/運用詳細](docs/system-design-20261026.md)を参照します。

MinecraftとHertaを統合し、各サービスの現在状態と24時間タイムラインを返します。Hertaから120秒以上データを受信できない場合は、最後の正常状態を維持せず`unknown`へ変更します。

内部受信API：

```text
POST /api/internal/status-ingest
```

HMAC-SHA256、timestamp、request ID、本文hashで認証し、SQLiteでリプレイを検知します。詳細は次を参照してください。

- `docs/status-api-schema.md`
- `docs/status-ingest-auth.md`
- `docs/system-design-20261026.md`（基本/詳細設計・ER・AS-IS/TO-BE・運用/テスト/受入）
- `docs/oci-deployment.md`

## UIのフォールバック

`/api/status.json`がまだ存在しない場合、UIは既存collectorの次のJSONを読み込みます。

```text
/api/current.json
/api/history.json
```

## ローカルUI確認

```bash
python3 -m http.server 4173
```

## Status APIテスト

```bash
cd services/status-api
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
pytest
```

## 静的UIの検証

```bash
python3 scripts/validate.py
node --check assets/app.js
bash -n scripts/deploy-oci.sh
bash -n scripts/rollback-oci.sh
```

## GitHub Actions

| Workflow | 役割 |
|---|---|
| `UI Quality Check` | HTML、JavaScript、UIデプロイスクリプト検証 |
| `Deploy to OCI` | 静的UIをOCIへデプロイ |
| `Status API Quality Check` | FastAPIテスト、linux/arm64 Docker build、スクリプト検証 |
| `Deploy Status API to OCI` | GHCRへARM64イメージをpushし、OCIへ`--no-build`デプロイ |

Status APIの自動デプロイは、GitHub Repository Variable `STATUS_API_AUTO_DEPLOY=true` の場合だけ`main`へのpushで実行します。初回OCI設定が完了するまでは、Actions画面の手動実行を使用してください。

GitHub Environment `production` では次のSecretsを利用します。

| Secret | 内容 |
|---|---|
| `OCI_HOST` | OCIインスタンスのホスト名またはIP |
| `OCI_USER` | 通常は`opc` |
| `OCI_SSH_KEY` | デプロイ専用Ed25519秘密鍵 |
| `OCI_KNOWN_HOSTS` | OCIホストのknown_hosts行 |

Herta受信用の共有鍵はGitHubへ保存せず、OCI上の次へ登録します。

```text
/opt/ivrm/compose/ivrm-status-api/.env
```

## 静的UIをOCIへ手動デプロイ

```bash
bash scripts/deploy-oci.sh
```

既存UIは次へ退避されます。

```text
/opt/ivrm/www/stats.ui-backup-YYYYMMDD-HHMMSS
```

ロールバック：

```bash
bash scripts/rollback-oci.sh /opt/ivrm/www/stats.ui-backup-YYYYMMDD-HHMMSS
```
