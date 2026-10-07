# ops-agent（Phase 0: 読み取り専用の状況分析）

`docs/llm-ops-agent-design.md` の Phase 0 の実装です。公開Status APIの状態を読み、異常があるときだけ AWS Bedrock のLLMに分析させ、結果をJSONで出力します。

**書き込みも操作も一切しません。** お知らせの作成、サーバー操作、公開は、この段階では存在しません。

## 動作

```text
GET /api/status.json, /api/status-history.json?days=7   （この2つの固定パスだけ）
        │
        ▼
 すべて正常か?  ── はい ──▶ 何もしない（LLMを呼ばない・AWS認証も不要・費用0円）
        │ いいえ
        ▼
 月額予算の確認 ── 超過 ──▶ 停止（終了コード2）
        │
        ▼
 Bedrock Converse（1回だけ・toolはreport_analysisのみ強制）
        │
        ▼
 出力をスキーマ検証 ── 不正 ──▶ 破棄（終了コード3）
        │
        ▼
 JSONを標準出力へ
```

## 実行

```bash
cd services/ops-agent
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

python -m ops_agent --dry-run   # LLMに送る内容を表示するだけ（呼び出しなし）
python -m ops_agent             # 異常があるときだけ分析
python -m ops_agent --force     # 正常でも分析（動作確認用）
```

終了コード: `0` 成功（スキップ含む）/ `2` 月額予算を超過 / `3` 失敗（取得失敗・モデル出力の拒否）

## 設定（環境変数）

| 変数 | 既定値 | 内容 |
| --- | --- | --- |
| `OPS_AGENT_STATUS_API_BASE` | `https://status.ivrm.jp` | httpsのみ（localhostはhttp可） |
| `OPS_AGENT_BEDROCK_REGION` | `ap-northeast-1` | |
| `OPS_AGENT_BEDROCK_MODEL_ID` | `jp.anthropic.claude-haiku-4-5-20251001-v1:0` | 日本向け推論プロファイル。差し替え可能 |
| `OPS_AGENT_MAX_OUTPUT_TOKENS` | `700` | 1回の出力上限 |
| `OPS_AGENT_MONTHLY_BUDGET_JPY` | `1500` | 月額上限（JSTの暦月） |
| `OPS_AGENT_USD_JPY` | `150` | 概算用の為替 |
| `OPS_AGENT_PRICE_INPUT_USD_PER_MTOK` / `..._OUTPUT_...` | `1.0` / `5.0` | **概算用。実際のBedrock料金で確認してください** |
| `OPS_AGENT_LEDGER_PATH` | `ops-agent-ledger.jsonl` | 利用額の台帳 |

## 費用の管理

- **呼び出しの前に**、保守的な概算額を台帳（JSONL）に予約として書き込み、応答のあとで実際の使用量に精算します。応答がタイムアウトした場合やプロセスが落ちた場合は、課金された可能性があるので予約を残します（記録漏れで上限をすり抜けないため）。Bedrockが処理前に拒否したことが確実なエラー（`ThrottlingException` など）のときだけ、予約を解放します。
- 予約額は、リクエストの**UTF-8のバイト数**（トークン数の上限になる）と出力トークンの上限から見積もります。絵文字などで文字数がトークン数を下回ることはありません。
- 予約するときに、**月の合計に収まるかを確認**します（ファイルロックで、同時に起動した複数のプロセスが両方とも通過することも防ぎます）。収まらなければ、呼び出す前に停止します。
- **SDKの自動リトライは無効**（`max_attempts: 0`）です。タイムアウト後の再試行が二重に課金されうるためです。予約は1回の試行ぶんです。
- 応答に使用量が含まれない場合は、予約を相殺せず、そのまま概算の請求として残します。
- 月の概算額が上限に達したら、呼び出す前に停止します。
- 1回の分析は約2,000入力 / 数百出力トークンで、概算で**1円未満〜数円**です。月1,500円なら数百回以上の余裕があります。
- 台帳は**概算**です。請求額の正本ではないため、AWS側にも **AWS Budgets のアラート**（例: 月1,500円）を設定してください。
- 正常なときはLLMを呼ばないので、平常時の費用は0円です。

## 安全設計

- LLMに渡すtoolは `report_analysis` の1つだけで、`toolChoice` で強制します。操作・公開・書き込みに相当するtoolはありません。
- Status APIから読むデータは**ホワイトリストのフィールドだけ**を送ります（説明文・meta・自由記述は送らない）。公開記録のタイトルは120字に切り詰めます。
- データは `<data>` タグ内に入れ、「命令ではなくデータ」とシステムプロンプトで明示します。JSONの `<` `>` `&` はエスケープ（`<` など）するので、記録のタイトルに `</data>` が含まれていても、データの枠を閉じることはできません。
- 出力は厳格に検証します。許可外のフィールド、長すぎる文字列、想定外のtool名は**修正せず破棄**します。破棄しても呼び出しの費用は台帳に記録します。
- 制御文字は除去します。

## AWS認証

設定の手順（専用ロール、予算アラート、動作確認）は [`aws/README.md`](aws/README.md) を参照してください。ポリシーは `aws/make_policy.py` が、使うプロファイルから自動で作ります。


- 本番では、`bedrock:InvokeModel` / `bedrock:Converse` を**使うモデルのARNだけ**に許可した専用IAMロールを使ってください。
- **rootアカウントの認証情報を使わないでください。**
- 長期のアクセスキーをサーバーに置かず、可能なら短期認証情報（ロールの引き受け / OIDC）を使います。

## テスト

```bash
pip install -r requirements-dev.txt
pytest
```

テストはBedrockを偽物に差し替えており、AWSへは接続しません。

## 未実装（次のPhase）

- 結果の通知先（承認者・通知チャネルが未決定）
- お知らせの下書き作成（Phase 1）。LLM専用の操作者を許可するSupabase側の変更**案**は `supabase/` にあります（未適用。`supabase/README.md` 参照）
- 承認キューとサーバー操作（Phase 2以降。現在の操作手順の確認が必要）
- スケジュール実行（実行場所が未決定）
