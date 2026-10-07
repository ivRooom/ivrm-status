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
2. 異常（`degraded` / `outage` / `unknown`）は、**5分以上続いたとき**に初めて動きます。開発中の短い再起動では動きません。
3. **進行中のメンテナンスの対象サービス**は、無視します（そのサービスだけ。ほかのサービスの異常は通常どおり扱います）。
4. 同じ異常は、**1回だけ分析**します。続く間は、1時間ごとに「継続中」の短い通知（LLMなし）だけです。
5. 回復したら、「回復」を1回通知して、状態を消します。
6. 手動の一時停止（`--silence 分`）の間は、すべて止まります。
7. LLMが使えない、または月額の予算に達したときは、**AIなしの短い通知**を送ります（公開はしません）。
8. 通知に失敗したときは、次の実行で**分析のやり直しなしに**再送します。

通知は、承認者へのDiscordのダイレクトメッセージです。**メンションは無効**にしてあり、AIが作った文章で、誰かに通知が飛ぶことはありません。設定がないときは、通知の内容をジャーナルに出します。

## 通知先

「両方」と決まったうちの、2つ目と1つ目です。

| 通知先 | 状態 |
| --- | --- |
| **DiscordのDM** | **実装済み**（このPR）。専用のボットが必要 |
| **管理画面の一覧** | **未実装。`ivrm-web` 側の作業**です。下の契約を提案します |

### 専用のDiscordボットが必要です

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
3. ホストにPython 3.11を入れる（Amazon Linux 2023: `sudo dnf install -y python3.11 python3.11-pip`、`pip3.11 install boto3`）。標準のPythonは3.9ですが、boto3の対応のため3.11を使います。
4. 配置:

   ```text
   /opt/ivrm-ops-agent/ops_agent/            このリポジトリの services/ops-agent/ops_agent/
   /etc/ivrm-ops-agent/env                   deploy/env.example をもとに作成（chmod 600、root所有）
   /etc/ivrm-ops-agent/aws-config            deploy/aws-config.example をもとに作成（<ACCOUNT_ID> を置き換える）
   /etc/systemd/system/ivrm-ops-agent.service
   /etc/systemd/system/ivrm-ops-agent.timer
   ```

5. ユーザーを作る: `sudo useradd -r -s /sbin/nologin ivrm-ops-agent`
6. まず**手で1回**実行して、動作を確認する。

   ```bash
   sudo -u ivrm-ops-agent env $(sudo cat /etc/ivrm-ops-agent/env | xargs) \
     AWS_CONFIG_FILE=/etc/ivrm-ops-agent/aws-config AWS_PROFILE=ops-agent \
     OPS_AGENT_STATE_PATH=/tmp/ops-state.json PYTHONPATH=/opt/ivrm-ops-agent \
     python3.11 -m ops_agent --watch
   ```

7. 有効化する: `sudo systemctl daemon-reload && sudo systemctl enable --now ivrm-ops-agent.timer`
8. 一時停止（作業中など）: `python3.11 -m ops_agent --silence 60`（1〜1440分）

## 確認できていないこと

- **本物のDiscordへの送信**（ボットがありません）。送信の仕組みは、偽のDiscordのAPIに対するテストで確認しています。
- **systemdのユニットの実機での起動**。`systemd-analyze verify` では、エラーは出ませんでした。セキュリティの設定（`ProtectSystem=strict` など）が、実機のBedrock・Discordへの通信を妨げないかは、実行して確認が必要です。
- **ロールの引き受け**（`credential_source = Ec2InstanceMetadata`）。AWSには、何も作成していません。
- 台帳の月額の概算は、請求額の正本ではありません（AWS Budgetsと併用してください）。
