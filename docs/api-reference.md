# hems-api API リファレンス

設定・動作環境・免責事項は [README](../README.md) を参照。

## 実行境界と失敗契約

GET（`/status`、`/security/status`）はSelenium workerを呼ばず、background refresherが作ったsnapshotを即時に返す。background read commandは6秒、制御（`/control`、`/security/lock`、`/security/shutter`）はHTTP受付時から25秒の絶対deadlineで処理する。worker timeout時のprocess group回収には最大2秒を加える。Home Assistant側はREST sensorを10秒、`rest_command`を30秒としている。

snapshotはworkerが検出した各階の`aircon:<floor>`と`security`を独立管理し、60秒周期で更新する。worker再生成で階一覧が変わると、消えた階のsnapshotと更新待ちを破棄する。直近refreshが成功し観測から90秒未満の値だけ`200`とし、未取得・直近失敗・90秒以上では古い状態bodyを返さず`503 HEMS snapshot unavailable`とmetadataを返す。失敗時は最後の成功値と観測時刻を内部で維持するが公開せず、次の成功時だけ復旧する。

controlは実行中の1 refresh完了後に1件だけ待機でき、後続refreshより優先される。追加controlは`503`でqueueしない。workerへframeを送ったcontrolは、単一対象の確認済み200をseedできた場合を除いて対象状態を優先refreshするが、制御を自動再送せず、要求値や未検証のcontrol responseをsnapshotへ直接反映しない。HTTP応答後にHome Assistantが行うread-onlyな`homeassistant.update_entity`で、実機の観測値を再取得する。

制御の絶対期限は受付から25秒で、worker内のstage境界は累積してT+10秒（接続・画面・フロア）、T+15秒（電源）、T+18秒（mode/temp送信）、T+24秒（最終状態取得、HTTP応答用1秒確保）とする。どのstageでも期限を越えたら後続操作を行わず、結果未確定の`503`を返す。

同一時点の結果は、次の優先順位で確定する。

1. 認証・権限: キー欠如/不一致は`401`、READキーで制御系を呼んだ場合は`403`。
2. JSON・入力検証: 不正なContent-Type、JSON、パラメータは`400`。認証後、レート制限より先に検証する。
3. capability検証: 階一覧・防犯機器構成が未確定なら`503`、無効化した錠は`403`、未搭載機器は`501`。workerやレート制限窓を消費しない。
4. runtime利用可能性: controlの追加待機、worker起動/回収中、生成失敗、deadline超過は`503`。これは制御結果が未確定であることを示す。GETはruntime admissionへ入らない。
5. security制御のレート制限: 検証を通過して受理された`/security/lock`・`/security/shutter`の3秒以内の連投は`429`。`503`のbusy/runtime unavailableはレート制限窓を消費しない。

| 状況 | 応答 | 意味とクライアント動作 |
|---|---:|---|
| 認証情報がない/不正 | 401 | キーを確認する。自動再送しない。 |
| READキーで制御系を呼ぶ | 403 | CONTROLキーと権限を確認する。 |
| JSONまたは入力が不正 | 400 | 本文を修正してから送る。実機操作は開始されていない。 |
| 検出階が未確定 (`reason=floors_unknown`) | 503 | worker ready前のため空調の読取・制御を行わない。 |
| 防犯構成が未取得/stale (`reason=capabilities_unknown`) | 503 | 錠・シャッターを制御せず、freshな防犯snapshotを待つ。 |
| 電気錠を無効化済み (`reason=lock_disabled`) | 403 | 設定による明示拒否。workerへ要求を送らない。 |
| 錠/シャッターが未搭載 (`reason=*_not_installed`) | 501 | 対象機器がないためworkerへ要求を送らない。 |
| OFF→ON後の確認不能 (`reason=off_to_on_unverified`) | 503 | 電源ONへの遷移後にmode/tempを確認できない。`actual`はworkerが観測した値であり、再送せずGETで確認する。 |
| stage/HTTP絶対期限超過 (`reason=control_deadline_exhausted`) | 503 | 結果未確定。`actual`が含まれる場合も要求値ではなく観測値であり、再送せずGETで確認する。 |
| worker起動/回収中、busy、生成失敗 (`reason=backend_unavailable`) | 503 | workerが制御結果を返せない。frame送信前後を問わず自動再送せず、GETで確認する。 |
| snapshot未取得、直近refresh失敗、観測から90秒以上 | 503 | 古い状態bodyは返さない。`snapshot.last_error`と次回refreshを確認する。 |
| lock/shutterの受理済み連投 | 429 | 3秒窓が空くまで待つ。503とは異なり、受理済み要求のレート制限である。 |
| Selenium操作は完了したが要求値と最終観測値が不一致 (`502`) | 502 | `requested`/`actual`/`failed`を確認する。操作が機器へ届いた可能性があるため、再送前にGETで確認する。 |
| 想定外の内部欠陥 (`500`, 通常は`internal error`) | 500 | サーバーログとGETを確認し、無条件再送しない。 |

特に`503`は、タイムアウトした操作がHEMS機器へ届かなかったことを意味しない。空調・鍵・シャッターの制御で`503`を受けた場合は、同じ操作を再送する前にGETで実状態を取得し、必要な場合だけユーザー判断または安全な手動操作へ進む。APIやHome Assistant automationは、タイムアウトを理由に制御を自動再送しない。

制御結果のsnapshot seedは、確認済み`200`のbodyと、`502`または`off_to_on_unverified`のbodyに含まれるworker観測`actual`だけに限定する。`control_deadline_exhausted`、`backend_unavailable`、`actual`のない`502`はseedしない。これにより、要求値を現在値と誤表示せず、失敗後のHA更新でも観測値を優先できる。

## エンドポイント

全リクエストに `X-API-Key` ヘッダーが必要。キーは READ/CONTROL の2種類:

- GET系（`/status`, `/security/status`）: `HEMS_API_KEY_READ` または `HEMS_API_KEY_CONTROL` のいずれでも許可
- POST系（`/control`, `/security/lock`, `/security/shutter`）: `HEMS_API_KEY_CONTROL` のみ許可

キー欠如・不一致は `401`、キーはあるが権限がない場合（READキーでPOST系を叩いた等）は `403`。`/security/lock`, `/security/shutter` は3秒以内の連投で `429`（レート制限）を返す。

| Method | Path | 説明 |
|--------|------|------|
| GET | `/status?floor=<n>` | 検出済み階のエアコンfresh snapshot。`floor`は必須 |
| GET | `/security/status` | 鍵・シャッター状態取得 |
| POST | `/control` | エアコン操作 |
| POST | `/security/lock` | 鍵操作 |
| POST | `/security/shutter` | シャッター操作 |

### GET /status

```bash
curl -H "X-API-Key: <read_key>" http://<api-host>:5000/status?floor=1
```

レスポンス例:
```json
{
  "floor": 1,
  "mode": "暖房",
  "temperature": 22.0,
  "power": "ON",
  "floors": [1, 2],
  "snapshot": {
    "observed_at": "2026-08-27T06:00:00Z",
    "age_seconds": 2.4,
    "stale": false,
    "last_error": null
  }
}
```

`floors`は現在のworkerがコントローラー画面から検出した正整数の階一覧。認証済みで`floor`を省略、または一覧にない階を指定した場合は`400`。worker未readyで階一覧が未確定なら`503 {"reason":"floors_unknown"}`。未認証なら入力検証より先に`401`を返す。

### GET /security/status

```bash
curl -H "X-API-Key: <read_key>" http://<api-host>:5000/security/status
```

レスポンス例:
```json
{
  "lock": "LOCKED",
  "shutter": "CLOSED",
  "capabilities": {"lock": "available", "shutter": "available"},
  "snapshot": {
    "observed_at": "2026-08-27T06:00:04Z",
    "age_seconds": 1.1,
    "stale": false,
    "last_error": null
  }
}
```

`lock`は`LOCKED|UNLOCKED|UNKNOWN|NOT_INSTALLED|DISABLED`、`shutter`は`OPEN|CLOSED|UNKNOWN|NOT_INSTALLED`。`capabilities.lock`は`available|not_installed|disabled`、`capabilities.shutter`は`available|not_installed`を返す。

利用不可時の共通例（状態bodyは含めない）:

```json
{
  "error": "HEMS snapshot unavailable",
  "snapshot": {
    "observed_at": "2026-08-27T05:58:00Z",
    "age_seconds": 124.2,
    "stale": true,
    "last_error": "stale"
  }
}
```

### POST /control

```bash
curl -X POST -H "X-API-Key: <control_key>" -H "Content-Type: application/json" \
  -d '{"floor": 1, "mode": "暖房", "temp": 22, "power": "ON"}' \
  http://<api-host>:5000/control
```

パラメータ（すべて省略可）。POST本文は `application/json` のJSONオブジェクトでなければならず、空ボディ・構文不正・配列などの非オブジェクトは `400` を返す。未知フィールドは将来の拡張との互換性のため無視する。バリデーション違反（範囲外の値等）も `400` を返す:

| パラメータ | 型 | 値 |
|-----------|----|----|
| `floor` | int | `floors` に含まれる検出済み階。省略時は全検出階を更新 |
| `mode` | string | `暖房` `冷房` `除湿` `自動` `送風` |
| `temp` | number | `17` 〜 `30` の整数℃（`20.0` は可、`20.5` は不可） |
| `power` | string | `ON` または `OFF` |

`temp` は、実機（スマート・エアーズ）が1F・2Fとも1℃刻みのみ対応するため整数限定であり、Home Assistant の `temp_step: 1` / `precision: 1.0` とも一致する。

`floor` 指定時、フロア切替自体が確認できない場合（ボタン不在かつ現在フロアが不一致、または切替タイムアウト）は、mode/temp/power の操作を一切実行せず即座に `502` を返す:

```json
{
  "error": "Floor switch not confirmed",
  "requested": {"floor": 1}
}
```

フロア切替が確認できた場合のみ、以降 mode/temp/power の操作へ進む。操作コマンド送信後、Selenium 操作の失敗（DOM要素不在・クリック失敗・反映タイムアウト）または最終状態が要求値と一致しない場合は `502` を返す:

```json
{
  "error": "Control not confirmed",
  "requested": {"mode": "暖房", "temp": 22, "power": "ON"},
  "actual": {"floor": 1, "mode": "冷房", "temperature": 22.0, "power": "ON"},
  "failed": ["mode"]
}
```

- `requested`: リクエストで指定された `mode` / `temp` / `power`（未指定なら `null`）
- `actual`: `get_current_status()` が返した実際の最終状態
- `failed`: 反映されなかった項目名の配列（`mode` / `temp` / `power` / `floor` のいずれか）。`power == "OFF"` 指定時は `mode`/`temp` は比較対象外（既存挙動通り無視される）
- 一致すれば従来通り `200` で状態を返す

### POST /security/lock

```bash
curl -X POST -H "X-API-Key: <control_key>" -H "Content-Type: application/json" \
  -d '{"action": "lock"}' \
  http://<api-host>:5000/security/lock
```

`action`: `lock` または `unlock`

`HEMS_DISABLE_LOCK=true`なら`403 {"reason":"lock_disabled"}`、未搭載なら`501 {"reason":"lock_not_installed"}`、構成snapshotが未取得/staleなら`503 {"reason":"capabilities_unknown"}`。いずれもworkerへ送らずレート制限窓を消費しない。

### POST /security/shutter

```bash
curl -X POST -H "X-API-Key: <control_key>" -H "Content-Type: application/json" \
  -d '{"action": "open"}' \
  http://<api-host>:5000/security/shutter
```

`action`: `open` または `close`

未搭載なら`501 {"reason":"shutter_not_installed"}`、構成snapshotが未取得/staleなら`503 {"reason":"capabilities_unknown"}`。いずれもworkerへ送らずレート制限窓を消費しない。

## セットアップ

### 環境変数 (.env)

```env
HEMS_URL=http://<hems-device-host>
HEMS_USER=<hems-user>
HEMS_PASSWORD=<password>
HEMS_API_KEY_READ=<generated_key>
HEMS_API_KEY_CONTROL=<generated_key>
HEMS_DISABLE_LOCK=false
```

APIキー生成:
```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

### 起動

```bash
# ビルド＆起動（初回またはコード変更後）
docker compose up --build -d

# 設定変更をコンテナ再作成で反映
docker compose up -d

# ログ確認
docker compose logs -f hems-api
```

### ステータス確認

```bash
# 認証なし → 401
curl -s -o /dev/null -w "%{http_code}" http://<api-host>:5000/status

# 認証あり → 200
curl -s -H "X-API-Key: <read_key>" http://<api-host>:5000/status?floor=1
```

## 脅威モデル

- hems-api は信頼できるプライベートネットワーク内で稼働させ、必要な場合はリバースプロキシやVPNでアクセスを制限する
- APIキーは平文HTTP（TLSなし）で送信されるため、インターネットへ直接公開せず、TLS終端または信頼できるネットワーク経由で利用する
- 残存リスク: ネットワークへ侵入した攻撃者による盗聴でキーが漏えいする可能性
- 代替の緩和策:
  - ポート5000への到達元を Home Assistant と管理端末に IP allowlist で制限（`DOCKER-USER` iptablesチェーン）
  - READ/CONTROLキー分離による権限分離
  - `/security/lock`・`/security/shutter` のレート制限
  - 入力値検証（floor/mode/temp/power）

## Home Assistant 連携

設定例を自分の Home Assistant 設定へ取り込み、API hostを置換する。

APIキーは `secrets.yaml` に `hems_api_key_read` / `hems_api_key_control` を定義し、`configuration_hems.yaml.example` からは `!secret` 参照で読み込む方式に変更済み（平文キーはコミットしない）。

- `sensor` (rest platform) × 3: 1F/2Fエアコン・セキュリティ状態を60秒間隔で取得
- `sensor` のHTTP timeoutは3件とも10秒。GETはcache参照だけで即時応答し、background readの6秒deadlineとは分離される
- `rest_command` × 3: エアコン制御・鍵・シャッター操作。HTTP timeoutは3件とも30秒（APIの制御command deadline 25秒、HTTP上限27秒より後に切断する）
- `mqtt climate` × 2: 1F/2FエアコンをHAのClimateエンティティとして公開（`optimistic: false`）。
- `automation`: MQTT ↔ REST API のブリッジ。4つの空調command automationは`rest_command.hems_control`を1回だけ呼び、`continue_on_error: true`でHTTPエラー後も直後のread-onlyな`homeassistant.update_entity`を1回だけ実行する。delay/retry/queueは持たず、制御を自動再送しない。REST sensorが`unknown`/`unavailable`、空調の期待`floor`不一致、または`power`が`ON`/`OFF`以外の場合はretainを更新しない。空調modeは、`OFF`ならmode/temperature欠落でも`off`をpublishし、`ON`ならmode allowlist（暖房/冷房/除湿/自動/送風）を要求する。温度は`ON`かつ暖房/冷房/自動で数値の場合だけpublishし、除湿/送風など温度非対応時はmodeだけ更新して直前の温度retainを保持する。securityはlockとshutterを別々の`if` guardでpublishするため、lockが`NOT_INSTALLED`/`DISABLED`でも有効なshutter状態は更新する。

HAの正本とリポジトリのレビュー用コピーを比較してから、別途承認を得た場合だけ正本へ反映する。正本反映後は **Developer Tools → YAML → Reload all YAML** を実行する。

管理コピーの構文確認（正本へ書き込まない）:
```bash
yq . configuration_hems.yaml.example >/dev/null
```
