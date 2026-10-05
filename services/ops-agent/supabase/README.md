# LLM専用の操作者（Phase 1の前提）

`ai-draft-actor.sql` は、**お知らせ・Incident・Maintenanceの「下書き作成」だけ**をLLM専用の操作者に許可する、Supabase `ivrm-core` への変更案です。**まだ本番に適用していません。**

## なぜ単純に `ai_agent` を許可しないのか

`ivrm-core` の status RPC は9個あり、**すべて同じ検証関数 `status_actor_valid_v1` を共有**しています（役割は `administrator` / `owner` のみ許可）。

| 種類 | RPC |
| --- | --- |
| 作成（draft） | `create_status_announcement_v1` / `create_status_incident_v1` / `create_status_maintenance_v1` |
| 公開 | `publish_status_announcement_v1` / `publish_status_incident_v1` / `publish_status_maintenance_v1` |
| 更新・中止・アーカイブ | `append_status_incident_update_v1` / `cancel_status_maintenance_v1` / `archive_status_announcement_v1` |

この関数に `ai_agent` を足すと、**LLMが公開や中止までできてしまいます。** 「公開は人が行う」という方針に反するため、次の形にします。

1. `status_ai_draft_actor_valid_v1` を新設。許可するのは固定の身元1つだけ（role `ai_agent` / email `ops-agent@ivrm.invalid` / Discord IDなし）。
2. **作成の3関数だけ**が、従来の検証 **または** 新しい検証を受け入れる。公開・更新・中止・アーカイブの6関数は変更せず、`ai_agent` を拒否し続ける。

変更は、各関数の現在の定義の「検証の1行」を書き換える形です。その行がちょうど1箇所でなければ**中断**するので、本番の定義が想定とずれていても黙って壊れません。

## 守りの境界は `api.ivrm.jp`

これらのRPCは `service_role` だけが実行できます。つまり「操作者」は**呼び出し側が渡す引数**にすぎず、本当の境界は呼び出し側のサーバー（`api.ivrm.jp`）です。

- LLM（ops-agent）に渡す認証情報は、**下書き作成用の専用エンドポイントにしか届かない**ものにする。
- そのエンドポイントは、操作者を `ai_agent` に**固定**し、`create_*` だけを呼ぶ。操作者や関数名を呼び出し側から指定させない。
- `service_role` のキーをops-agentに渡してはならない。

このSQLは、そのサーバー側に不具合があっても、公開まで進めないための**二重の守り**です。サーバー側の実装は `ivRooom/ivrm-web` で行います（このリポジトリの対象外）。

## 追加で必要なもの（未実装）

- 下書きの量の制限（例: 1時間あたりの件数、未公開の下書きの上限）。**DBではなく `api.ivrm.jp` で行う**（DB側では操作者を区別して数える材料がないため）。
- 管理画面で「AI下書き」と分かる表示。監査ログ（`audit_logs`）の `actor_role = 'ai_agent'` から判別できる。
- 下書き作成用のエンドポイントとops-agent側のクライアント。

## テスト

```bash
bash services/ops-agent/supabase/test-ai-draft-actor.sh   # Dockerが必要
```

使い捨てのPostgreSQLを起動し、次を確認します。**本物のデータベースには接続しません。**

- 適用前は、`ai_agent` が全RPCで拒否される。
- 適用後は、`ai_agent` が**作成の3つだけ**成功し、公開・更新・中止・アーカイブの6つは拒否される。
- 管理者は、これまでどおり全RPCで成功する（退行なし）。
- 身元は固定: 別のemail、別の役割、Discord ID付きは、作成でも拒否される。
- もう一度適用すると、二重に書き換えずに中断する。
- 新しい検証関数は、`public` / `anon` / `authenticated` から実行できない。

テストのフィクスチャは、本番の `status_actor_valid_v1` の定義と、**検証行が本番と同じ**スタブ関数を使います。本物のRPCの本体（`audit_logs`への記録など）は含まないため、**本番の定義に対する確認ではありません。**

## 適用する前に

1. **Supabaseのブランチ（開発用の複製）に先に適用して**、実際のRPCで試す。ブランチの作成は有料です（時間課金）。
2. 適用後の確認クエリ（`ai-draft-actor.sql` の末尾）を実行する。特に、`publish_status_announcement_v1` が `ai_agent` を拒否すること。
3. マイグレーションは、`ivrm-core` を管理しているリポジトリの流れ（`ivrm-web` など）に載せる。このファイルは**提案**です。
