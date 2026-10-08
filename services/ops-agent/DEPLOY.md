# ops-agent を動かす場所と、通知

## 決定: AWSの `ivrm-app-01`（EC2）で、systemdのタイマー（5分ごと）

調査（AWSの構成を読み取りだけで確認）をもとにした判断です。

| 場所 | 認証 | 台帳（予算の記録） | 判断 |
| --- | --- | --- | --- |
| **`ivrm-app-01`（EC2）** | **インスタンスプロファイル。長期のキーなし** | ディスクに残る | **採用** |
| OCI | 長期のアクセスキーが必要 | ディスクに残る | 見送り |
| Lightsail `ivrm-herta` | インスタンスロールを使えない（長期のキーが必要） | ディスクに残る | 見送り |
| GitHub Actions（OIDC） | キーなし | **実行ごとに消える**（予算の上限が効かなくなる）。リポジトリが公開で、ログも公開 | 見送り |
| Lambda + EventBridge | ロール | 別の保存先（DynamoDBなど）が必要 | 将来の選択肢 |

- 正常なときは、**LLMも、AWSも呼びません**。5分ごとの実行は、公開のステータスを読むだけです。
- 注意: `ivrm-app-01` は本番のAPIと**同じホスト**です。この処理は軽いものですが、同じホストなので、**APIのコンテナが乗っ取られると、ここから専用ロールを引き受けられます**（その場合の被害は、Bedrockの費用までで、AWS Budgetsのアラートと台帳の上限で限定されます）。気になる場合は、Lambdaへの移行を検討してください。
- 注意: 公開のステータスを読めないとき（ホストのネットワーク障害など）は、通知が出ません。外部からの死活監視（既存のLambdaプローブ）が、別にあります。

## 動作（`python -m ops_agent --watch`）

5分ごとに1回実行され、次の順で判断します。

1. 異常がなければ、何もしません（費用0）。
2. 異常（`degraded` / `outage` / `unknown`、または未解決のIncident）は、**5分以上続いたとき**に初めて動きます。開発中の短い再起動では動きません。同じサービスの異常は、状態が変わっても（停止↔一部影響など）**1つの出来事**として数えます。
3. **進行中のメンテナンスの対象サービス**は、無視します（そのサービスだけ。ほかのサービスの異常は通常どおり扱います）。通知済みの異常のあとでメンテナンスが始まった場合は、**「回復」とは言わず**、静かに終了します（メンテナンスのあとも続いていれば、新しい出来事として、もう一度数えます）。
4. 同じ異常は、**1回だけ分析**します。分析のときだけ、7日分の履歴も取得します（履歴のAPIが失敗しても、履歴なしで分析します）。続く間は、1時間ごとに「継続中」の短い通知（LLMなし）だけです。
5. 回復したら、「回復」を1回通知して、状態を消します。別のサービスの異常が始まって出来事が置き換わるときも、先に、回復したサービスの「回復」を通知します。
6. 手動の一時停止（`--silence 分`）の間は、すべて止まります。
7. LLMが使えない、または月額の予算に達したときは、**AIなしの短い通知**を送ります（公開はしません）。
8. 通知に失敗したときは、次の実行で（そのとき、サービスが回復していても）**分析のやり直しなしに**再送します。

通知は、**承認者だけが入れるDiscordの非公開チャンネルへのWebhook**（推奨）か、承認者へのダイレクトメッセージ（専用のボットが必要）で送ります。どちらも、**メンションは無効**にしてあり、AIが作った文章で、誰かに通知が飛ぶことはありません。設定がないときは、通知の内容をジャーナルに出します。

## 通知先

「両方」と決まったうちの、2つ目と1つ目です。

| 通知先 | 状態 |
| --- | --- |
| **Discordの非公開チャンネル（Webhook）** | **実装済み。推奨。** ボットは不要 |
| **DiscordのDM** | 実装済み（任意）。専用のボットが必要 |
| **管理画面の一覧** | **未実装。`ivrm-web` 側の作業**です。下の契約を提案します |

### Webhook（推奨。ボットは不要）

Webhookのいちばんの利点は、**権限が最小**なことです。そのURLでできるのは、**そのチャンネルへの投稿だけ**です（Hertaの内部APIや、ボットのトークンのように、サーバーの管理まで奪われることはありません）。また、**Hertaから独立**しているので、Hertaが止まっても、通知は届きます。

1. あなただけが入れる**非公開のチャンネル**を作る（例: `#ops-alerts`）。チャンネルの通知を「すべてのメッセージ」にしておくと、DMと同じように気づけます。
2. チャンネルの設定 → 連携サービス → ウェブフック → 新しいウェブフック → 「ウェブフックURLをコピー」。
3. `/etc/ivrm-ops-agent/env` に、`OPS_AGENT_DISCORD_WEBHOOK_URL=` として書く（root専用。**URLに秘密が含まれます**。リポジトリ、チャット、ログには書かないでください）。
4. Webhookを使うときは、ボットの設定（`OPS_AGENT_DISCORD_USER_ID` / `OPS_AGENT_DISCORD_BOT_TOKEN`）は**書かないでください**。両方あると、どちらが本物か分からないので、起動を拒否します。
5. URLが漏れたときは、Discordで**そのWebhookを削除して作り直し**、envを更新します（漏れたURLは、すぐに無効になります）。

Hertaの内部API（メッセージ送信）は、使いません。`127.0.0.1` でしか待ち受けておらず（別のホストから呼べない）、ロールの削除やボットのプロフィールの変更も同じ秘密でできてしまうためです。

### DMを使う場合: 専用のDiscordボットが必要です

- **Hertaのトークンを流用しないでください**（別のホストに、秘密の置き場所が増えるため）。ops-agent専用の、アプリケーションを作ります。
- ボットは、**あなたと同じサーバーに参加**している必要があります。そして、あなたの設定が「サーバーメンバーからのDMを許可」になっている必要があります（なっていないと、DiscordがDMを開けません）。
- 取得したトークンは、`/etc/ivrm-ops-agent/env` にだけ置きます（root専用）。あなたのユーザーIDも、同じファイルです（リポジトリには置きません）。

### 管理画面の一覧（提案する契約）

`ivrm-web` に、ops-agentが書き込める専用の入口を作る場合の提案です。

- `POST /internal/ops/notices`（機械用の資格情報。下書き作成と同様に、**この入口にだけ届く権限**）。
- 本文: `{ "kind": "analysis" | "fallback" | "reminder" | "recovered", "services": [...], "since": "...", "analysis": {...} | null, "dedupe_key": "<異常の識別子>" }`。
- `dedupe_key` で冪等にします。管理画面は、承認待ちの一覧に表示します（サーバー操作の提案を足すときも、同じ入口を使えます）。
- これは**契約の提案**で、ops-agent側は、まだ書き込みません（入口が存在しないため）。

## 導入（あなたが実行）

1. **IAMのロール** `ivrm-ops-agent` を作る。
   - 権限: `aws/make_policy.py` の出力（Bedrockの `InvokeModel`、プロファイル経由のみ）。
   - 信頼ポリシー: `aws/role-trust-policy.example.json`（ホストのインスタンスロールだけが引き受けられる）。
2. ホストのインスタンスロールに、`aws/host-assume-role-policy.example.json` を足す（`ivrm-ops-agent` だけを引き受けられる）。
3. 配置（**先に**、コードと設定のファイルを置きます。次の手順の `pip install` が、`requirements.txt` を使います）:

   ```text
   /opt/ivrm-ops-agent/ops_agent/            このリポジトリの services/ops-agent/ops_agent/
   /opt/ivrm-ops-agent/requirements.txt      このリポジトリの services/ops-agent/requirements.txt
   /etc/ivrm-ops-agent/env                   deploy/env.example をもとに作成（chmod 600、root所有）
   /etc/ivrm-ops-agent/aws-config            deploy/aws-config.example をもとに作成（<ACCOUNT_ID> を置き換える）
   /etc/systemd/system/ivrm-ops-agent.service
   /etc/systemd/system/ivrm-ops-agent.timer
   ```

4. Python 3.11を入れ、**専用の仮想環境**に依存を入れる（Amazon Linux 2023）。標準のPythonは3.9ですが、boto3の対応のため3.11を使います。

   ```bash
   sudo dnf install -y python3.11
   sudo python3.11 -m venv /opt/ivrm-ops-agent/venv
   sudo /opt/ivrm-ops-agent/venv/bin/pip install -r /opt/ivrm-ops-agent/requirements.txt
   ```

   サービスは、専用のユーザーで、`-s`（ユーザー領域を使わない）つきで動きます。そのため、`pip install boto3` を一般のユーザーで実行しても、サービスからは**読み込めません**（正常なときは、boto3を読み込まないので、最初の実行では気づけず、最初の障害の分析で、初めて失敗します）。必ず、この仮想環境に入れてください。

5. ユーザーを作る: `sudo useradd -r -s /sbin/nologin ivrm-ops-agent`
6. まず**通知の試験**をします。障害を待たずに、本物のDiscordへの送信を確認できます（モデルは呼ばないので、費用はかかりません）。環境ファイルを、サービスと同じ方法で読み込みます。

   ```bash
   sudo systemd-run --wait --pipe --uid=ivrm-ops-agent \
     -p EnvironmentFile=/etc/ivrm-ops-agent/env -E PYTHONPATH=/opt/ivrm-ops-agent \
     /opt/ivrm-ops-agent/venv/bin/python -s -m ops_agent --test-notify
   ```

   Discordのチャンネルに、「通知の試験」が届き、`{"test_notify": "sent", ...}` が出れば成功です。`notify_failed` のときは、HTTPの状態だけが出ます（`401` / `403` / `404` はURL・権限の誤り、`429` は短時間の送りすぎです）。

   次に、**サービスを1回**実行して、動作を確認する。本番と同じユニット（サンドボックスと環境ファイルを含む）で実行されるので、設定ファイルのコメント行などで困りません。

   ```bash
   sudo systemctl daemon-reload
   sudo systemctl start ivrm-ops-agent.service
   sudo journalctl -u ivrm-ops-agent.service -n 30 --no-pager
   ```

   正常なときは、ジャーナルの最後に `{"action": "healthy", ...}` が出ます。`notify_misconfigured` や `status_unavailable` が出たら、設定を確認してください。

   **これだけでは、AWSの設定は確認できません**（正常なときは、boto3もロールも使わないため）。続けて、本番と同じ環境で、AWSの準備を確認します。モデルは呼ばないので、費用はかかりません。

   ```bash
   sudo -u ivrm-ops-agent env PYTHONPATH=/opt/ivrm-ops-agent \
     AWS_CONFIG_FILE=/etc/ivrm-ops-agent/aws-config AWS_PROFILE=ops-agent \
     /opt/ivrm-ops-agent/venv/bin/python -s -m ops_agent --check
   ```

   `{"check": "ok", ...}` が出れば、boto3を読み込め、ロールを引き受けられています。`check_failed` のときは、理由（読み込めないパッケージ、認証情報の欠落など）が出ます。

7. 有効化する: `sudo systemctl daemon-reload && sudo systemctl enable --now ivrm-ops-agent.timer`
8. 一時停止（作業中など。1〜1440分）。**本番の状態ファイルを指定して、サービスと同じユーザーで**実行します（指定しないと、成功と表示されても、タイマーには効きません）。

   ```bash
   sudo -u ivrm-ops-agent env PYTHONPATH=/opt/ivrm-ops-agent \
     OPS_AGENT_STATE_PATH=/var/lib/ivrm-ops-agent/state.json \
     /opt/ivrm-ops-agent/venv/bin/python -s -m ops_agent --silence 60
   ```

   出力の `silenced_until_epoch` が、一時停止の終了時刻です。一時停止は、状態ファイルとは別のファイル（`state.json.silence`）に書かれるので、タイマーの処理が動いている最中に実行しても、互いに上書きしません。一時停止の間も、未送信の通知は、再送しません（終了後に送ります）。

## 確認できていないこと

- **本物のDiscordへの送信**（WebhookのURLも、ボットもありません）。送信の仕組みは、偽のDiscordのAPIに対するテストで確認しています。最初の本物の送信は、手順6のあと、障害を待たずに確認できます（下の「通知の試験」）。
- **systemdのユニットの実機での起動**。`systemd-analyze verify` では、エラーは出ませんでした。セキュリティの設定（`ProtectSystem=strict` など）が、実機のBedrock・Discordへの通信を妨げないかは、実行して確認が必要です。
- **ロールの引き受け**（`credential_source = Ec2InstanceMetadata`）。AWSには、何も作成していません。
- 台帳の月額の概算は、請求額の正本ではありません（AWS Budgetsと併用してください）。
