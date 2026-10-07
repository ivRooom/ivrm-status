# ops-agent のAWS設定（専用ロールと予算アラート）

**この手順は、あなた自身のAWS認証情報で実行してください。** このリポジトリは、AWSには何も作成・変更しません。

> 重要: **rootアカウントの認証情報を使わないでください。** ops-agentには、下の専用ロールだけを使います。

## 1. 使うモデルの確認（読み取りのみ）

`jp.` のようなクロスリージョン推論プロファイルは、**プロファイル自体と、宛先リージョンすべてのモデル**に許可が必要です。宛先は、プロファイルから自動で取得します。

```bash
cd services/ops-agent
pip install -r requirements.txt
python aws/make_policy.py > ops-agent-policy.json     # bedrock:GetInferenceProfile を呼ぶだけ
```

出力は、次の2つの文です。

| 文 | 内容 |
| --- | --- |
| `InvokeThroughTheProfile` | プロファイルのARNへの `bedrock:InvokeModel` |
| `InvokeDestinationModelsOnlyViaTheProfile` | 宛先のモデルのARNへの `bedrock:InvokeModel`。**このプロファイル経由のときだけ**（条件 `bedrock:InferenceProfileArn`） |

- ワイルドカード（`*`）は出力しません。プロファイルの情報が取れなければ、**推測せずに失敗**します。
- モデルを変えるときは、`OPS_AGENT_BEDROCK_MODEL_ID` を変えて、もう一度実行します。
- 出力を**必ず読んでから**使ってください（宛先のリージョンが想定どおりか）。

## 2. 専用のIAMロール

1. `ops-agent` 専用のロールを作り、`ops-agent-policy.json` をアタッチする。他の権限は付けない。
2. **長期のアクセスキーを作らない**。実行する場所に応じて、次のどちらかにします。
   - AWS上で動かす → そのサービスのロール（EC2・ECS・Lambda など）。
   - OCIなど外部で動かす → 短期の認証情報（IAM Roles Anywhere、または引き受けるロールのセッション）。**どこで動かすかが未決定**なので、決まってから選びます。
3. ロールの信頼ポリシーは、**呼び出し元を絞る**（特定のロール・証明書だけ）。

## 3. 予算アラート（AWS Budgets）

ops-agentの台帳は**概算**で、請求額の正本ではありません。AWS側にも上限の警告を置きます。

- 月の予算: 1,500円相当（Budgetsは**USD建て**。為替で換算してください。約10 USDが目安）。
- 通知: 実額が80%と100%、予測が100%。通知先は、あなたのメールアドレス。
- 範囲: Bedrockのサービスだけに絞ると、他の費用と混ざりません。
- **Budgetsは使用を止めません**（通知だけ）。止めるのは、ops-agentの台帳です。

作成は、AWSコンソールの「Billing and Cost Management → Budgets」から行えます。

## 4. 動作確認

ロールの認証情報で、1回だけ実行します（費用は1円未満）。

```bash
python -m ops_agent --force
```

- 成功 → JSONで分析が出る。
- `AccessDeniedException` → 宛先のモデルの許可が足りない。手順1をやり直す。
- 終了コード `3` と `analysis_failed` → 理由は標準エラーに出る（認証情報やリクエストの中身は出ません）。

## 確認できていないこと

- このポリシーで実際に `Converse` が成功するかは、**AWSに接続できなかったため未確認**です（`make_policy.py` の出力の形はテスト済みですが、AWSの評価結果は未確認）。手順4で確認してください。
- 条件キー `bedrock:InferenceProfileArn` の挙動は、AWSのドキュメントにある方式に従っています。手順4で失敗した場合は、この条件を外して試し、結果を教えてください（外すと、宛先のモデルを直接呼べるようになるので、その場合は理由を確認してから使うこと）。
- 料金（`OPS_AGENT_PRICE_*`）は概算の既定値です。最新の料金で更新してください。
