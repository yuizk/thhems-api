# HEMS API

[English](README.md) | [日本語](README.ja.md)

トヨタホーム / デンソー「スマート・エアーズ」全館空調および対応するHEMSコントローラーWeb UI（サポートされている電気錠・電動シャッターを含む）向けのFlask APIとHome Assistant統合です。本プロジェクトは非公式のコミュニティプロジェクトです。動作可否はコントローラーの機種、ファームウェア、およびWeb UIの仕様に依存します。機器メーカーとの提携やメーカー公式のサポートはありません。

## アーキテクチャ

Home Assistant → 認証付きFlask API → 単一Seleniumワーカー → コントローラーWeb UI。
読み取りリクエストはバックグラウンドで取得されたスナップショットを利用します。制御リクエストは優先権を持ち、一定の制限時間（デッドライン）が設けられています。Dockerビルド時にChromeおよび適合するChromeDriverがインストールされます。本イメージは Linux amd64 を対象としています。

## ローカルでの起動

```sh
git clone https://github.com/yuizk/hems-api.git
cd hems-api
cp .env.example .env
# .env を編集: 各プレースホルダーをご自身の環境設定値に置き換えてください
docker compose config --quiet
docker compose build --no-cache
docker compose up -d
```

コントローラーのURL、ログインID、パスワード、および独立した READ / CONTROL 用の各APIキーは必須です。2つの独立したランダムなキー（例: `openssl rand -hex 32` など）を生成してください。`HEMS_DISABLE_LOCK` は任意で、既定値は `false` です。`.env` はコミットしないでください。APIが起動すると、バックグラウンド読み取りのために設定されたコントローラーへの接続が行われます。

デフォルトのポートバインドはローカルホストからの接続のみを許可します。別ホストにある Home Assistant から接続する場合は、信頼できるLANインターフェースへ意図的にバインドするか、TLSリバースプロキシとファイアウォールを使用してください。APIキーは通信を暗号化しないため、APIをインターネットへ直接公開しないでください。READキーはGETリクエストのみを許可し、CONTROLキーは読み取りと機器の物理制御の両方を許可します。詳細は [APIリファレンス](docs/api-reference.md) を参照してください。

Compose設定はブラウザ実行に必要な `seccomp:unconfined` および Chrome の `--no-sandbox` 要件を維持しています。信頼できるホスト・ネットワーク上で実行してください。

## Home Assistant

`configuration_hems.yaml.example` をパッケージ／設定例として使用してください。`hems-api.example.invalid` をAPIホストに置き換え、Home Assistant の `secrets.yaml` に `hems_api_key_read` と `hems_api_key_control` を設定します。この例では Home Assistant の MQTT 統合とブローカーが必要です。既存の設定とマージし、リロード前に YAML の構文チェックを実施してください。

この設定例では、HTTPリクエストが失敗した場合でも空調コマンド実行後に状態を再取得し、検証済みのスナップショットのみを保持します。タイムアウトが発生した場合でも、機器側にはコマンドが届いている可能性があります。再試行する前に現在の状態を確認してください。エラー発生時に制御コマンドを自動で再送しないでください。

### 電気錠がない／使わない場合

APIはログインセッションごとに、コントローラーが提示する階数・電気錠・シャッターを検出します。`GET /status` が返す `floors` に合わせて、`configuration_hems.yaml.example` の階別 REST sensor・MQTT climate・automation ブロックを増減してください。未搭載の防犯機器は `NOT_INSTALLED` として表示し、制御しません。

搭載済みの電気錠をAPIから操作させない場合は `HEMS_DISABLE_LOCK=true` を設定します。防犯ステータスでは `DISABLED` と表示し、施錠・解錠要求を拒否します。電気錠がない場合または無効化する場合は、Home Assistant 設定例の電気錠用 REST command・MQTT lock・automation ブロックを削除してください。シャッターのpublishは電気錠と独立して継続します。

## 開発とイメージ検証

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m pytest -v
python -m ruff check .
docker build --no-cache -t hems-api:smoke .
scripts/smoke-image.sh hems-api:smoke
```

スモークテストは `--network none` かつダミーの認証情報で実機Chromeを実行し、HEMSコントローラーへは一切ログインしません。CIではテストとリントを実行し、週次ワークフローで最新Stable Chromeをビルドしてこのスモークテストを実行します。メンテナーのリリースワークフローは1回ビルドし、そのイメージでスモークテストを実行し、イメージIDを比較した上で同一イメージをプライベートGHCRパッケージへpushします。一般利用者はローカルでビルドするため、メンテナーのパッケージへのアクセス権は不要です。

## 免責事項とライセンス

本ソフトウェアは、空調設備、電気錠、および電動シャッターを物理的に操作できます。誤った設定、サポートされていない機器の利用、または障害によって、予期せぬ機器の動作、セキュリティの低下、または損害が生じる恐れがあります。機器の互換性、安全な運用、アクセス制御、および手動による復旧手段の確保は利用者自身の責任となります。メーカー純正の物理コントローラー・操作手段を常に利用可能な状態にしておいてください。本ソフトウェアに関していかなる保証や安全認証も提供されません。

[MIT License](LICENSE) の下で公開されています。
