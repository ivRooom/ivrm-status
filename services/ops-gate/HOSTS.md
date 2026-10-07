# ops-gate: ホストごとの導入と、管理画面からの実行経路

このゲートは**2つのホスト**に置きます。操作の対象が別のホストにあるためです。

| ホスト | 場所 | ゲートで許可する操作 | 設定の例 |
| --- | --- | --- | --- |
| OCI | Minecraft（mc-main / mc-resource）、status.ivrm.jp | `start_mc_resource`、`restart_mc_main` | `config.oci.example.json` |
| Lightsail `ivrm-herta` | Herta（bot / worker） | `restart_herta` | `config.lightsail.example.json` |

- **ホストごとに、別の署名の共有鍵を使ってください。** 1つのホストが侵害されても、別のホストのチケットを偽造できないようにするためです。
- 承認者のDiscordユーザーIDは、各ホストの `/etc/ivrm-ops-gate/config.json` にだけ書きます（リポジトリには置きません）。
- Pythonは**3.9以上**で動きます（OCIホストの基準が3.9）。標準ライブラリだけで、追加のインストールは不要です。CIは3.9と3.13で実行します。

## 3つの操作

| 操作 | 実体 | 完了の判定 |
| --- | --- | --- |
| `start_mc_resource` | `docker compose up -d`（`/opt/ivrm/compose/minecraft-resource`） | `mc-resource` が running（healthcheckがあればhealthy） |
| `restart_mc_main` | `docker compose restart`（`/opt/ivrm/compose/minecraft-main`） | `mc-main` が running（healthcheckがあればhealthy） |
| `restart_herta` | `docker compose ... restart bot worker`（`/app/herta`） | `bot` と `worker` が running、かつ `http://127.0.0.1:3000/healthz` が応答 |

- どれも、`ops/*.sh` が**実行と確認までを1つにまとめた**スクリプトです。確認に失敗するとゲートは「失敗」を返すので、管理画面は「実行したが、正常に戻らなかった」と分かります。
- **再起動は1回だけです。** 確認の段階では、読み取りのコマンドしか実行しません。
- スクリプトは、アプリのディレクトリに移動せず、**絶対パス**で `docker compose` を呼びます（ゲートは別のディレクトリから動くため）。
- 置き場所を変える場合は、環境変数（`IVRM_MC_RESOURCE_DIR`、`IVRM_MC_MAIN_DIR`、`IVRM_HERTA_DIR`、`IVRM_HERTA_HEALTH_URL`）ではなく、**スクリプトの場所と設定の `argv`** で管理してください（ゲートは環境変数を渡さないため）。

### 生活鯖の再起動の事前チェック

`restart_mc_main` は、**プレイヤーが1人でもつないでいると拒否します**（再起動は全員を切断するため）。`status.ivrm.jp` の公開APIで人数を調べますが、APIは、ライブのデータがないときも `playersOnline` を返します（最後の記録、または0）。そのため、人数は**信頼できるときだけ**受け入れます。

- 観測が3分以内で、かつライブのプローブが応答している（`probeStatus` が `reachable`）→ **そのプローブが数えた人数だけ**を使う。0人のときだけ許可する。
- それ以外（古い、プローブが判定不能、時刻が読めない、など）→ **拒否**する。
- **応答しないサーバー（`unreachable` / 停止）も拒否します。** APIは、通信のタイムアウトや不正な応答も同じ「停止」として扱い、そのとき、確立済みのプレイヤーの接続が残っていることがあるためです。応答しないサーバーの再起動は、SSHで手動で行ってください。

拒否された場合は、状況を判断したうえで、これまでどおりSSHで手動で再起動してください。ゲートは、そのための経路ではありません。

## ホストへの配置

```text
/opt/ivrm-ops-gate/ops_gate/      このリポジトリの services/ops-gate/ops_gate/
/opt/ivrm-ops-gate/ops/           services/ops-gate/ops/
/opt/ivrm-ops-gate/run.sh         実行ビット付き
/etc/ivrm-ops-gate/config.json    ホストごとの設定の例をもとに作成
/etc/ivrm-ops-gate/ticket-secret  chmod 600。作り方: openssl rand -hex 32
/var/lib/ivrm-ops-gate/           状態（使用済みチケットなど）
/var/log/ivrm-ops-gate/           監査ログ
```

`/opt/ivrm-ops-gate` は、実行するユーザー以外が書き込めないようにします（ここに書き込めると、実行するコマンドを差し替えられます）。

実行するユーザーは、`docker` を呼べる必要があります。

- **管理画面からSSM経由で実行する場合:** SSMのエージェントが root で実行します。追加の設定は不要です。
- **SSHの固定コマンドで実行する場合:** 専用のユーザー（例: `opsgate`）を作り、`docker` グループに入れます。`docker` グループは root 相当の権限なので、**ログインシェルは使えないようにし、固定コマンドだけ**にしてください（`authorized_keys` の `restrict,command=...`）。あなた自身の管理用の鍵とは、別の鍵にします。

## 管理画面から実行する経路: SSM Run Command（推奨）

### 判断

管理画面・API（`api.ivrm.jp`）は、すでにAWSで動いています。そこから各ホストのゲートを呼ぶ方法を比べました。

| 方法 | 長所 | 短所 |
| --- | --- | --- |
| **SSM Run Command（推奨）** | **Lightsail `ivrm-herta` は、すでにSSMのマネージドインスタンスとして登録済み（Online）。** `ivrm-web` のデプロイも、同じ仕組み（GitHub OIDC + SSM Run Command）を使っている。SSHの鍵をクラウドに置かない。受信の接続口を増やさない。IAMで「この文書を、このホストにだけ」と絞れる。CloudTrailに記録が残る | **OCIは、まだSSMに登録されていない**（ハイブリッドアクティベーションが必要） |
| SSHの固定コマンド | OCIにも、すぐ使える。デプロイで、すでにOCIへSSHしている | 秘密鍵をクラウドの秘密情報として持つ。**Lightsailの22番ポートは、全世界に開いている**（後述） |
| ホストが取りに行く（pull） | 受信の接続口が不要 | 各ホストに常駐のエージェントと、キューを読む資格情報が新しく必要。待ち時間が出る |

**結論:** SSM Run Commandにします。ゲート自体は変わりません（同じチケットの検証）。

- **Herta（Lightsail）:** すぐに使えます。
- **OCI:** SSMのハイブリッドアクティベーションを、1回だけ行う必要があります（下記）。それまで、管理画面からのOCIの操作は、できません。LLMの提案と承認は動きますが、**OCIの2操作は、あなたがSSHで実行する**運用になります。

### 仕組み

SSMの文書 `ivrm-ops-gate-run`（`aws/ssm-document.json`）は、**ゲートだけを呼びます**。受け取るのは次の3つで、SSMが形式を先に検証します。

| 引数 | 検証 |
| --- | --- |
| `verb` | `run` または `check` だけ |
| `ticket` | `^[A-Za-z0-9_-]{1,4096}$` |
| `signature` | `^[0-9a-f]{64}$` |

シェルの記号（引用符、`;`、`$`、空白、改行など）は、どの引数にも入れられません。そのうえで、ゲートがもう一度、チケットを検証します。

呼び出す側のIAM権限（`aws/caller-policy.example.json`）は、次のとおりです。

- `ssm:SendCommand` は、**この文書**と、**列挙したホストだけ**。`AWS-RunShellScript` など、ほかの文書には、送れません。
- `ssm:GetCommandInvocation`（結果の読み取り）。
- **`ssm:SendCommand` には、ワイルドカードはありません**（文書とホストを列挙）。結果を読む `ssm:GetCommandInvocation` だけは、AWSがリソース単位の制限を提供していないため `Resource: "*"` です（読み取りのみで、実行はできません）。`ssm:StartSession` や `ssm:*` は、ありません。

呼び出す側が侵害されても、**署名の共有鍵を持たなければ、チケットを偽造できません**。共有鍵を持つ承認サービスが侵害された場合は、許可リストの3操作（クールダウンつき）までが、被害の上限です。

### OCIをSSMに登録する手順（あなたが実行）

Herta（Lightsail）で使った方法と同じです。

1. IAMロール（例: `ivrm-oci-ssm-hybrid-role`）を作り、`AmazonSSMManagedInstanceCore` をアタッチする。信頼ポリシーは、`ssm.amazonaws.com` が引き受けられるもの。
2. SSMの「ハイブリッドアクティベーション」を作る（有効期限は短く、登録は1台）。
3. OCIホストにSSMのエージェント（arm64）をインストールし、アクティベーションのコードとIDで登録する。
4. 管理画面のコンソールで、マネージドインスタンスが「Online」になることを確認する。
5. 呼び出す側のIAM権限に、そのインスタンスIDを足す。

### 文書の作成（あなたが実行）

```bash
aws ssm create-document --name ivrm-ops-gate-run --document-type Command \
  --document-format JSON --content file://aws/ssm-document.json
```

**このリポジトリは、AWSには何も作成しません。** 文書の実際の動作（SSMでの実行、結果の取得）は、AWSに作成してから確認する必要があります。最初は `verb=check` で試してください（何も実行せず、チケットも消費しません）。

## 気になった点（このゲートの範囲外）

- **Lightsail `ivrm-herta` の22番ポート（SSH）が、`0.0.0.0/0` に開いています**（調査で読み取りました）。SSMが使えるので、SSHを閉じるか、あなたのIPだけに絞ることを検討してください。ここは私が変更していません。
