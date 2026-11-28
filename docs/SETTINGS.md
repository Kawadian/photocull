# Web 設定画面

レビュー画面または写真ブラウザから **設定** を開きます。画面には 12 のカテゴリがあり、日本語のラベル、キー名、説明、環境変数名で横断検索できます。カテゴリで **詳細設定** を開くと、通常は変更しない項目も表示されます。スマートフォンではカテゴリをセレクトで切り替えます。

項目を編集し、**差分を確認** からサーバーが検証した変更を確認して **設定を保存** を選びます。**既定値に戻す** はその項目のアプリ既定値を編集欄に設定します。**取り消し** は編集中の変更を破棄します。**変更履歴** では日時を選んで差分を確認し、その版を新しいリビジョンとして復元できます。保存後、再起動が必要な変更があるとバナーが表示されます。ホスト側の仕組みをセットアップすると、**適用（再起動）** から処理を開始できます。ブラウザは API の再起動中に接続を待ち、完了または失敗を表示します。

## 管理者アクセス

Photocull の既存の共有トークンを `token_file` または `PHOTOCULL_TOKEN_FILE` で有効にしてください。ファイルは API ユーザーが読み取れる配備用のファイルとして用意し、Compose では API へ読み取り専用でマウントします。トークンを設定していない場合、設定 API は無効です。

ユーザーごとのロールはありません。トークンを知っているクライアントと、リバースプロキシからトークンを付与される接続は管理者として扱われます。Caddy や Nginx から既存の `X-Photocull-Token` を付与する場合は、管理者に認証された接続に限定してください。公開側から受け取った同名ヘッダーはプロキシで置き換えます。既存の Host 許可リストを利用し、プロキシでは元の Host を転送します。設定の変更、パス検証、接続テストは既存の Origin と Fetch Metadata の検査を通ります。共有トークン利用の構成例は README を参照してください。

## 保存先と優先順位

優先順位は **対応する環境変数 → UI の上書き → photocull.yaml → アプリの既定値** です。環境変数が優先される項目には変数名と **env で上書き中** が表示されます。API とワーカーの環境が異なる場合、それぞれのプロセスに設定された変数が優先されます。個別ライブラリの判定プロバイダーを既存のダイアログで選択した場合は、そのライブラリで個別設定が優先されます。

上書きファイルは、元の YAML または環境変数から解決したデータ保存先の `settings/active.json` にあります。Compose の既定ではホストの `.photocull/state/settings/active.json` に対応し、バックアップは同じディレクトリの `history/<UTC タイムスタンプ>-<ID>.json` に残ります。Web UI に表示する履歴は直近 100 件です。UI で `state_dir` を変更しても上書きファイルの場所は変わりません。データのコピーや移動は自動では行われません。

元の YAML と UI の上書きを分けているため、読み取り専用マウントのまま、元ファイルのコメントや順序を保てます。保存時は構成全体を再検証し、同時保存をリビジョンで検出し、前の版をバックアップしてから一時ファイルをアトミックに置き換えます。履歴は UI が保存した上書きの版であり、復元時にも現在の YAML、環境変数、パス制限が適用されます。配備設定や YAML を編集した場合は API とワーカーを再起動してください。

## パスを選択する

パスの入力欄から **参照** を開きます。ライブラリとその追加ルート、既存のキャッシュ、および運用側で追加した許可ルートを閲覧できます。追加する場合は API の `PHOTOCULL_SETTINGS_ROOTS` に、コンテナから見える絶対パスをコロン区切りで指定してください。例えばキャッシュや出力用の個別マウントを追加できます。マウントは運用側で用意します。トークンファイルを新しく選ぶ場合は、ライブラリ外に専用の許可ルートを指定します。

フォルダを辿るか、パスを手入力して **パスを検証** を選びます。API ユーザーからの読み取り可否と、必要な項目では書き込み権限を検査します。キャッシュを検証すると空き容量も表示されます。API とワーカーで権限やマウントが異なる場合は、ワーカー側にも必要な権限を付与してください。ブラウザは名前と種類を一覧するだけです。リンクは元の許可ルート内に解決される場合のみ辿れます。アーカイブの書き込みや隔離の実行には、引き続き既存のアーカイブ機能と安全確認が必要です。設定の保存と適用は状態ディレクトリにだけ書き込みます。

## 判定プロバイダーと egress

- 管理者が URL を編集できるようにするには、API とワーカーの `PHOTOCULL_SETTINGS_ENDPOINTS` に信頼するベース URL をコンマ区切りで指定します。元の YAML または env の有効なベース URL も信頼済みとして扱われます。信頼する相手には認証ヘッダーを送信します。HTTPS の利用と TLS の検証を前提にリモートの送信先を確認してください。UI の URL にはユーザー名、パスワード、クエリ、フラグメントを含められません。
- `PHOTOCULL_VLM_BASE_URL` と `PHOTOCULL_VLM_MODEL` が設定されている場合は、それらが優先されます。管理画面で変更する運用へ移行する場合は、信頼する URL を登録し、対応する環境変数を外して API とワーカーを再作成してから保存します。信頼リストから使用中の URL を外す前に、保存済みの URL を別の許可済み URL へ切り替えてください。
- `PHOTOCULL_VLM_API_KEY` はプロセスの環境変数だけで設定します。設定 API が返すのは設定済みかどうかです。
- **モデル一覧を取得** は OpenAI 互換の `/models` を API サーバーから呼び出します。OpenRouter 形式の入力モダリティと価格があれば一覧に表示します。**接続テスト** は合成した画像を送って画像入力が動作することを確認します。取得とテストには実際のリクエスト予算が適用されます。ワーカーからの疎通にはワーカーのネットワークと egress の設定も必要です。
- egress のホスト一覧を UI から反映するには、`deploy/compose.egress.yml` の後に `deploy/compose.settings-egress.yml` を追加します。先にホスト上の状態ディレクトリ内の `settings` を API の UID/GID で作成し、所有者に読み書きを許可します（推奨モードは `0700`）。プロキシにはそのディレクトリだけが読み取り専用で見えます。
- 空でない `PHOTOCULL_EGRESS_ALLOW` が優先されます。UI の許可リストを明示的に空にするとすべての接続を拒否します。環境変数と UI のどちらにも指定がない場合の既定値は `openrouter.ai` です。egress は引き続き許可された DNS ホストの 443 番ポートと公開 IPv4 アドレスのみに接続します。

## 次の処理から反映する判定設定

API とワーカーは、UI が保存した上書きファイルの変更を検知します。判定プロバイダーの既定値（`vlm.tags` と `vlm.flags`）、失敗時の動作（`vlm.on_failure`）、`vlm.openai` の一日・プロセスごとの上限、同時実行数、タイムアウト、分あたりの上限を読み直します。実行中の判定バッチは開始時の設定で完了します。再読込でプロセスごとのリクエスト使用数はリセットされません。ライブラリ個別のプロバイダー指定も継続して優先されます。

接続先の URL やモデルを変更したときは API とワーカーを再起動します。接続先の変更が未反映の間、新たな OpenAI 互換判定の有効化は再起動まで待ちます。ローカルへの切り替えは次の処理から反映します。YAML ファイルやプロセスの環境変数を変更した場合は、対象のサービスを再起動または再作成してください。

## 再起動が必要な項目

| 変更 | 反映方法 |
| --- | --- |
| 上記の即時反映対象以外の YAML 項目。エンドポイント、モデル、ブラウザのプライバシー設定も含む | 配備に使ったファイルとプロファイルを指定した `docker compose restart api worker`。systemd では `systemctl restart photocull-api photocull-worker` |
| UI で保存した egress 許可リスト | 上記のオーバーライドを追加した構成で `docker compose restart egress-proxy` |
| Compose のワーカーネットワーク、プロキシモード、環境変数、マウント | 配備に使った構成で `docker compose up -d`。必要に応じて配備のラッパーで `photocull-compose up -d` |
| systemd の環境変数やユニットの設定 | 配備側で編集し、ユニット定義を編集した場合は `systemctl daemon-reload` を実行してから対象ユニットを再起動 |

保存と復元のあとに **適用（再起動）** を選ぶと、対応するサービスを再起動します。受付の設定がない場合は、バナーに手動コマンドを表示します。マウント、起動環境、ネットワーク構成の変更は引き続きホスト側で行います。egress の手動再起動は UI から観測できないため、ホスト受付を経由するまで egress の変更が未確認として表示されます。

## ホストで「適用（再起動）」を有効にする

Linux のホストで、次の手順を運用管理者が一度行います。コンテナ側で必要な権限は、既存の状態マウント内にある要求ディレクトリへの書き込みです。ホストの systemd が配備済みのサービスを再起動します。

1. state マウントのホスト側の絶対パスと、API の UID/GID を確認します。以下は `/var/lib/photocull` と UID/GID `1000` の例です。Compose では `PHOTOCULL_STATE_HOST_DIR` の実際のパスを使います。

   ```sh
   state=/var/lib/photocull
   api_uid=1000
   api_gid=1000
   bridge="$state/settings/restart"
   sudo install -d -m 0700 -o "$api_uid" -g "$api_gid" "$state/settings"
   sudo install -d -m 0755 -o root -g root "$bridge" "$bridge/results"
   sudo install -d -m 0700 -o "$api_uid" -g "$api_gid" "$bridge/requests"
   ```

2. リポジトリのホスト用ヘルパーと unit をインストールします。

   ```sh
   sudo install -D -m 0644 photocull/restart.py /usr/local/libexec/photocull-settings-restart.py
   sudo install -m 0644 deploy/systemd/photocull-settings-restart.{path,service} /etc/systemd/system/
   ```

3. `/etc/photocull/settings-restart.env` を root が所有するファイルとして用意します。ラッパーとその親ディレクトリは root 所有にし、非 root ユーザーによる書き換えを禁止します。ラッパーが読み込む Compose ファイルと配備環境もホスト側で管理し、API ユーザーに書き込み権限を付与しないでください。ラッパーはホスト側で配備の作業ディレクトリ、Compose ファイルとプロファイルを選べる固定コマンドとし、`restart <service>` を受け取ります。標準 systemd 配備の場合は `api` と `worker` を対応する固定 unit にマップするラッパーを使用します。

   ```ini
   PHOTOCULL_RESTART_DIRECTORY=/var/lib/photocull/settings/restart
   PHOTOCULL_RESTART_WRAPPER=/usr/local/bin/photocull-compose
   PHOTOCULL_RESTART_SERVICES=api,worker
   ```

   egress の保存設定を使う Compose 構成では、ホストの一覧に `egress-proxy` を追加できます。`/var/lib/photocull` 以外の場合は、`systemctl edit photocull-settings-restart.path` のドロップインで `[Path]` の `PathExists=` を空にしてから実際の `requests/request.json` の絶対パスを追加し、同様に service の `[Service]` の `ReadWritePaths=` をリセットして実際のブリッジディレクトリを追加します。

4. API の配備環境で `PHOTOCULL_SETTINGS_RESTART_SERVICES=api,worker` を設定し、API を再作成または再起動して環境を反映します。egress を許可する場合はホスト側と同じように `egress-proxy` を追加します。ホストで path unit を有効にします。

   ```sh
   sudo systemctl daemon-reload
   sudo systemctl enable --now photocull-settings-restart.path
   ```

API は保存リビジョンと固定の対象サービスから要求を作り、`requests/request.json` をアトミックに公開します。ホストは要求 ID、時刻、サービスの許可リストを検証し、worker、egress-proxy、api の順に必要な対象だけを再起動します。結果は `results/result.json` に残ります。結果の成功はラッパーが成功したことを示します。API の表示は再接続できた時点で更新されます。権限や接続の設定を変えた場合は、新しいアドレスや認証経路から開き直してください。

ホスト受付が始まらない場合、画面に手動コマンドが表示されます。`systemctl status photocull-settings-restart.path photocull-settings-restart.service` でパスと権限を確認してください。修正後に path unit を再起動すると残っている期限切れ要求が処理され、次の要求を受け付けられるようになります。
