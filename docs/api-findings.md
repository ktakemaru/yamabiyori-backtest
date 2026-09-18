# API 調査結果 (Phase 0)

調査日: 2026-09-18 (JST 20:40〜21:30)。対象地点は唐松岳付近 lat=36.7595, lon=137.7503。
**ここに書いてあるのは、`probe/` のスクリプトで実際に投げたリクエストのレスポンス (`probe/raw/*.json`、ログ `probe/log_*.txt`) で確認できた事実だけ**。確認していないものは「未確認」と明記する。
公式ドキュメント (WebFetch で読んだ) の記述は「ドキュメント記載」と区別して書く。ドキュメント記載と実測がズレているものは両方書く。

---

## 0. 前提の食い違い (先に共有)

| 前提 | 実際 |
|---|---|
| プラグイン本体は `C:\yamabiyori` | **`C:\yamabiyori` は存在しない**。`ktakemaru/yamabiyori` のチェックアウトは `C:\mountain-weather` (branch `feature/backtest-harness`、未コミット変更あり)。本フェーズではここを読み取り専用として扱い、一切変更していない。 |
| バックテスト基盤は新規 | `C:\mountain-weather\tools\backtest\` に**既存の研究用ハーネス**がある (README 689行、Previous Runs 取得・JMA etrn スクレイパ・6地点バイアス監査・4モデルブレンド・擬似アンサンブル)。今回の Phase 0 と重なる知見が多い (§6 参照)。 |
| Single Runs API | 既存ハーネスは「`run=` は存在しない」と結論しているが、それは `historical-forecast-api` と通常 API に対する検証。`single-runs-api` は今回初めて検証し、**実在・動作を確認**。 |

---

## 1. Previous Runs API

- エンドポイント: `https://previous-runs-api.open-meteo.com/v1/forecast` — **確認済み** (HTTP 200)。
- パラメータ: `latitude, longitude, models, hourly, start_date, end_date, timezone` — 通常 forecast API と同じ形で動作 **確認済み**。
- 変数名: `<variable>_previous_day<N>` (N=1..7)、サフィックス無しは最新ラン (ドキュメント記載では `_previous_day0` = current run)。**確認済み**。
- 複数地点: `latitude=36.7595,35.3606,36.5758&longitude=...` で **リストが返る** (`location_id` 付き) **確認済み**。76座を1リクエストにまとめられる見込み (76地点同時は未確認)。
- 長期間: 1地点・2変数・**365日分 (8760h) を1リクエストで取得できた** (generationtime 503ms) **確認済み**。
- タイムゾーン: `timezone=Asia/Tokyo` で JST の時刻列が返る。アーカイブ境界日は 09:00 JST (=00Z) から値が入る (15/24 非null) → **アーカイブは UTC 日付境界で切れている** (観測から推定)。

### 1.1 モデル別: 取れた変数と previous_day の深さ (2026-09-10 の1日分で確認)

| 変数 | jma_msm | ecmwf_ifs025 | ecmwf_ifs (HRES 9km) |
|---|---|---|---|
| temperature_2m `_previous_dayN` | day1, **day2 まで** (day3+ 全null) | **day1〜7** (day8 全null) | **day1〜7** (day8 全null) |
| cloud_cover (全雲量) `_previous_dayN` | day1〜2 | day1〜7 | day1〜7 |
| precipitation `_previous_dayN` | day1〜2 | day1〜7 | day1〜7 |
| wind_speed_10m `_previous_dayN` | day1〜2 | day1〜7 | day1〜7 |
| **cloud_cover_low / mid / high `_previous_dayN`** | **全null** | **全null** | **全null** |
| wind_gusts_10m `_previous_dayN` | 全null (サフィックス無しも null、単位 `undefined`) | 全null (サフィックス無しは有り) | 全null (サフィックス無しは有り) |
| cape `_previous_day1` | 全null | 全null | 未確認 |
| precipitation_probability `_previous_day1` | 全null | 全null | 未確認 |
| **気圧面変数** (`cloud_cover_850hPa_previous_day1` 等) | **HTTP 400 で拒否** | **HTTP 400 で拒否** | **HTTP 400 で拒否** |
| 気圧面変数 (サフィックス無し `cloud_cover_850hPa`) | 取れる (=最新ランのみ) | 取れる | 未確認 |

要点:
- **雲量は「全雲量 (cloud_cover)」しかリードタイム別に遡れない。** low/mid/high は3モデルとも全null (ドキュメント記載でも Cloud Cover Total のみ)。
- **気圧面変数は Previous Runs API では扱えない** (変数名ごと拒否)。本体ヤマビヨリの「気圧面の雲量を山頂標高へ内挿」した値をリードタイム別に再現することは、この API ではできない。→ Single Runs API (§2) で代替。
- ecmwf_ifs (9km) はグリッドが違う (返る座標 36.731106, 137.71654; ifs025/msm は 36.75, 137.75)。返却 `elevation` は3モデルとも 2550m (格子標高ではなく要求地点付近の DEM 標高 — §8 参照)。
- ecmwf_ifs の初回リクエストは `generationtime_ms` ≈ 20,417ms と遅かった (2回目以降は数十ms)。コールドキャッシュの可能性、要注意。

### 1.2 データ開始日 (二分探索、`temperature_2m_previous_day1` の非null有無)

| モデル | 最初に値のある日 (JST) | ドキュメント記載 |
|---|---|---|
| jma_msm | **2022-07-02** (2022-07-01 は全null) | 「JMA GSM/MSM は 2018 から」 → **実測と不一致** |
| ecmwf_ifs025 | **2024-02-04** (02-03 は全null) | 「多くのモデルは 2024年1月から」 → 約1ヶ月遅い |
| ecmwf_ifs (9km) | **2025-10-01** (09-30 は全null) | 記載なし |

- ご認識の「多くのモデルで2024年1月から」は ecmwf_ifs025 では 2024-02-04、jma_msm は 2022-07-02 が実測。
- 未確認: `previous_day7` の開始日は day1 より最大6日遅れる可能性があるが、日単位では確認していない。
- 未確認: 上記は唐松岳の1格子点での結果。他地点で同じかは未確認 (アーカイブはモデル単位なので同じと推定)。

### 1.3 previous_dayN の正確な定義

- ドキュメント記載: 「`_previous_day1` は valid time の24時間前に予測された値、day2 は48時間前 … day7 まで」。
- **未確認**: 1日4ラン (00/06/12/18Z) のモデルで「24時間前」がどのランを指すのか (valid time − 24h 以前の最新ラン? 00Z ラン固定?) はレスポンスからは判別できない。リードタイムは「約 N 日」として扱い、時間単位の厳密なリードは Single Runs API で扱う、という前提を design.md に置く。

---

## 2. Single Runs API

- エンドポイント: `https://single-runs-api.open-meteo.com/v1/forecast` — **確認済み**。
- 必須パラメータ `run=YYYY-MM-DDTHH:MM` — **UTC 解釈** (エラー文が `run: 2026-09-01T03:00Z` と Z 付きで返る; 00Z 指定で時刻列が 09:00 JST から始まる) **確認済み**。`run` 無しは HTTP 400 `Parameter 'run' is required`。
- 通常 API (`api.open-meteo.com`) に `run=` を付けると HTTP 400 `Parameter 'run' must not be set` **確認済み** (既存ハーネスの結論と一致)。
- 複数モデル同時指定 (`models=ecmwf_ifs025,jma_msm`) → 変数名に `_ecmwf_ifs025` / `_jma_msm` サフィックスが付いて返る **確認済み**。
- 存在しないランは HTTP 400 `The requested model run is not available. Model: X, run: ...Z`。

### 2.1 モデル別: 利用可能なラン・ホライズン

| モデル | 最古のラン (実測) | ドキュメント記載 | ラン刻み (実測) | ホライズン (非null時間数) |
|---|---|---|---|---|
| ecmwf_ifs (HRES 9km) | **2024-03-14T00Z** (03-13 は不可) | 2024-03-14 → **一致** | 00/06/12Z 確認 (18Z 未確認) | 00Z/12Z: 168h (forecast_days=16 で **361h ≈ 15日**), 06Z: 145h |
| ecmwf_ifs025 | **2026-04-02T00Z** (04-01 は不可; 2024-03-14 も不可) | 「他モデルは 2026-04-02」→ **一致** | 00/06/12Z 確認 | 00Z: forecast_days=16 で 363h, 06Z: 147h |
| jma_msm | **2026-05-13T00Z** (05-12 は不可; 04-02/04-03 も不可) | 「他モデルは 2026-04-02」→ **不一致 (約6週間遅い)** | 00/03/06/12Z 確認 | 00Z/12Z: **79h** (≈78h+初期値), 03Z/06Z: **40h** (≈39h) |

- デフォルトの `forecast_days` は 7 (168h 返る)。ECMWF は `forecast_days=16` で 15日分まで非null。**16日目 (361〜384h) は null**。
- ご認識の「8日以上先のリード評価に使える」は ECMWF については **可能** (15日分)。jma_msm は 78h/39h までなので不可 (これは MSM の仕様)。

### 2.2 取れる変数 (run=2026-09-01T00Z で確認)

| 変数 | jma_msm | ecmwf_ifs025 | ecmwf_ifs (9km) |
|---|---|---|---|
| temperature_2m / cloud_cover / precipitation / wind_speed_10m | ○ | ○ | ○ |
| **cloud_cover_low / mid / high** | **○** | **○** | **○** |
| wind_gusts_10m | 全null | ○ (105/168、後半 null = 6h刻み?) | ○ |
| **気圧面** (cloud_cover/RH/wind_speed/temperature/geopotential_height @925/850/700hPa) | **○** | **○** | **全null** |
| cape | 全null | ○ | ○ |
| precipitation_probability | 全null | **リクエスト自体が HTTP 400** (内部で `ecmwf_ifs025_ensemble` に振られ「run not available」) | 同左 |

要点:
- **本体の標高内挿 (気圧面 → 山頂) をリードタイム別に再現できるのは Single Runs API の jma_msm (2026-05-13〜) と ecmwf_ifs025 (2026-04-02〜) だけ。** 蓄積は約4〜5ヶ月分しかない。
- ECMWF に `precipitation_probability` を混ぜると**リクエストごと失敗する**ので、変数リストから外す必要がある。
- 未確認: 850hPa 以外の本体が使う面 (1000/925/900/800/600hPa) の可否。本体 core.py の注記 (2026-09-09 検証) では通常 API で jma_msm は 1000〜600 全面、ecmwf_ifs025 は 900/800 が null とあり、Single Runs でも同じと推定するが未確認。

---

## 3. Historical Forecast API

- エンドポイント: `https://historical-forecast-api.open-meteo.com/v1/forecast` — **確認済み** (`start_date/end_date` 指定、3モデルとも HTTP 200)。
- cloud_cover_low/mid/high、wind_gusts (msm 以外)、気圧面 (未確認だが通常APIと同型) が取れる。
- **2026-09-10 の値は Previous Runs API のサフィックス無し (=最新ラン) と完全一致** (jma_msm: temp 4.9/4.8/4.8, cloud 90/93/91; ecmwf_ifs025: 4.7/4.7/4.8, 100/100/100)。→ Historical Forecast = 各時刻を「最も新しいラン」で埋めた系列 (ドキュメント記載: 「各ランの最初の数時間を繋いだ連続系列」)。
- ご認識の通り「疑似実況」としての利用は循環参照の懸念あり。**本設計では実況には使わず、Previous Runs の day0 と同義として扱う**か、Single Runs の短リード検証のクロスチェック用にとどめる。

---

## 4. エラー形式・レート制限

- エラーは全て **HTTP 400 + JSON `{"error": true, "reason": "..."}`** **確認済み**。
  - 不正変数: `Invalid value: Cannot initialize SurfacePressureAndHeightVariable<...> from invalid String value <文字列>` — **hourly 文字列全体がまとめて拒否される**ので、どの変数が原因かは分割して切り分ける必要がある。
  - 不正モデル: `Invalid value: Cannot initialize MultiDomains from invalid String value no_such_model`
  - ラン無し: `The requested model run is not available. Model: ..., run: ...Z`
- レスポンスヘッダに `X-RateLimit-*` / `Retry-After` 等の情報は**無かった** (保存したヘッダは `Content-Type` のみ)。
- **未確認**: 429 の挙動。本フェーズは約 120 リクエスト (1秒間隔) で、レート制限には一度も当たらなかった。
- ドキュメント記載 (terms): 非商用無料枠は **「1日 10,000、1時間 5,000、1分 600 コール未満」**、ライセンスは **CC BY 4.0**。帰属表示の具体的文言は terms ページには無い (別ページ参照とのみ) → **未確認**。キャッシュ/再配布の可否も terms ページに記載無し → **未確認**。

---

## 5. 気象庁アメダス

- `https://www.jma.go.jp/bosai/amedas/data/latest_time.txt` → `2026-09-18T21:10:00+09:00` (10分値の最新時刻) **確認済み**。
- `https://www.jma.go.jp/bosai/amedas/const/amedastable.json` → **1286 地点** **確認済み**。1地点のレコード例:
  `{"type":"C","elems":"11112010","lat":[45,31.2],"lon":[141,56.1],"alt":26,"kjName":"宗谷岬","knName":"ソウヤミサキ","enName":"Cape Soya"}`
  - lat/lon は `[度, 分]` の配列 (10進ではない)。`alt` は m。`type`: C=1131, B=95, A=56, D/E/F/G 各1。
  - `elems` 8文字フラグと map の実出現キーの対応 (実測):
    - `1xxxxxxx` → temp あり、`x1xxxxxx` → precipitation あり、`xx1xxxxx` → wind あり、`xxx1xxxx` → sun10m/sun1h あり (487+201+98+55+1 地点で一致)
    - 7文字目 `1` → pressure あり。5,6,8 文字目の意味は**未確認** (humidity/snow/visibility との対応が一意に取れなかった)。
    - 実務上は `elems` を解読するより、map JSON に出現するキーで地点の観測要素を判定する方が安全。
- 全国 10分値マップ `data/map/yyyyMMddHHmm00.json` → **確認済み** (1286地点、250〜350KB)。
  - 値は `[値, 品質フラグ]` の2要素配列 (例 `"temp":[17.6,0]`、欠測は `[null,5]`)。
  - 21:10 のキー: temp(916地点), humidity(842), **sun10m(843), sun1h(843)**, precipitation10m/1h/3h/24h(1285), windDirection/wind(915), pressure(154), normalPressure(151), visibility(151)
  - **正時 (21:00) のファイルには追加で** snow1h/6h/12h/24h(1286), snow(336), **weather(151)** が入る。`weather` は151地点 (=官署クラス) にのみ存在、値の意味は**未確認**。
  - **sun1h は 843 地点で取れる** → 「晴れていたか」ラベルの材料は map から直接取れる。
- 遡れる期間 (map, 21:00): 1/3/5/7/**8日前 → 200**、**10日前 → 404** (9日前は未確認)。14日・30日前も 404。→ **保持は約8〜9日**。日々収集が必要というご認識と一致。
- 地点別ファイル `data/point/<id>/yyyyMMdd_HH.json` (HH は 3 時間刻み: 00/03/…/21) → **確認済み**。3時間分の10分値 (最新ファイルは 2 レコード、7日前のは 8.3KB = 18レコード)。要素は map と同じ + `maxTemp/minTemp/gust/gustDirection` と時刻 (日累積の極値)。7日前・8日前は 200、10日前は 404 (map と同じ保持期間)。
- 未確認: JMA 側の利用規約は本フェーズで再確認していない (本体 SKILL.md には「公共データ利用規約 第1.0版に基づき出典表記が必要」と記載あり)。アクセス頻度の制限も未確認。

### 5.1 唐松岳 (36.7595, 137.7503) に近い観測所 (距離順)

| id | 名称 | alt | 距離 | 観測要素 (map 実出現) |
|---|---|---|---|---|
| 48141 | 白馬 | 703m | 12.0km | temp/humidity/sun/precip/wind (フル) |
| 54876 | 平岩 | 281m | 16.9km | precip のみ |
| 48097 | 小谷 | 550m | 17.4km | precip のみ |
| 55063 | 宇奈月 | 160m | 19.8km | precip のみ |
| 48146 | 鬼無里 | 778m | 20.7km | precip のみ |
| 55022 | 朝日 | 43m | 25.8km | フル |
| 48191 | 大町 | 784m | 27.2km | フル |
| 48156 | 長野 | 418m | 40.8km | type A (官署、pressure/visibility 含む) |

→ 唐松岳の「最寄り麓アメダス」は **白馬 (48141)** が唯一の現実的候補 (12km, 標高差 ≈2000m)。

### 5.2 標高の高い観測所 (全国、alt 順、上位)

| id | 名称 | alt | 唐松岳から | 観測要素 |
|---|---|---|---|---|
| 50066 | 富士山 | 3775m | 179km | type F。map では **temp と sun のみ** (21:10 時点 sun は `[null,5]` = 欠測フラグ)。wind/precip 無し |
| 48601 | 御嶽山 | 2195m | 101km | precip のみ |
| 48681 | 宮田高原 | 1660m | 110km | precip のみ |
| 48346 | 上高地 | 1510m | 58km | precip のみ |
| 48172 | 笠岳 | 1473m | 64km | precip のみ |
| 49052 | 乙女湖 | 1465m | 133km | precip のみ |
| 48436 | 白樺湖 | 1440m | 84km | precip のみ |
| **48571** | **野辺山** | **1350m** | 111km | **フル** |
| **41166** | **奥日光** | **1292m** | 156km | **フル (type B, pressure/visibility 含む)** |
| **48216** | **菅平** | **1253m** | 57km | **フル** |
| **42221** | **田代** | **1230m** | 72km | **フル** |
| **42121** | **草津** | **1223m** | 77km | **フル** |
| **36196** | **鷲倉** | **1220m** | 244km | **フル** |
| **48531** | **開田高原** | **1130m** | 92km | **フル** |
| **48466** | **奈川** | **1068m** | 75km | **フル** |
| **48561** | **原村** | **1017m** | 97km | フル |
| **52181** | **六厩** | **1015m** | 101km | フル |
| **48331** | **軽井沢** | **999m** | 85km | フル (type B) |
| **49256** | **山中** | **992m** | 177km | フル |

要点: **温度・風・日照が揃う地点で最も高いのは野辺山 1350m。1400m 超は富士山 (要素が特殊) を除き雨量計のみ。** 稜線・山頂の実況は富士山以外に存在しない (既存ハーネスの結論と同じ)。全リストは `probe/raw/amedas_stations_sorted.json`。

---

## 6. 本体コード (C:\mountain-weather) の構造要約

読み取りのみ。行番号は 2026-09-18 時点。

| 内容 | 場所 |
|---|---|
| **山リスト** `MOUNTAINS` (76座; name/lat/lon/elevation_m/access/region) | `mountain_weather_core.py` L88-173。唐松岳は `36.7514, 137.7622, 2696m` (今回のプローブ座標とは僅差)。region は粗い4区分 (関東甲信/伊豆/北陸信越/東北南部)。 |
| 気圧面スタック定義 `LEVEL_STACK_HPA=[1000,925,900,850,800,700,600]`, `LEVEL_KINDS=[cloudcover, relative_humidity, windspeed, winddirection, temperature, geopotential_height]` | core.py L228-231 |
| **標高内挿** `interp_at_altitude()`: geopotential_height を z 軸に線形内挿 (範囲外はクランプ、風向は近い面)、`add_altitude_columns()` で `cloudcover_at_summit` 等の列を合成 | core.py L306-339 |
| 登り層の湿り判定 `climb_layer_moist_series()` (山頂-600m の層、強制上昇ルール LIFT_MIN_WIND_MS=10 等) | core.py L289-440 |
| **スコア** `mountain_climb_score()`: cloud/visibility/precip の満足度の**重み付き幾何平均** (weights 0.25/0.25/0.50)。雷/風/低体温はスコア外の `mountain_hazards()` | core.py L705-770, L779-880 |
| `cloud_penalty()` = 雲量%そのまま。`precip_penalty(wet_pct, mm)`、`visibility_penalty()` | core.py L520-650 |
| 雨天時間判定 `is_wet_hour()`: MSM範囲は MSM 降水≥0.1mm または (RH≥90% かつ 雲≥40%)、MSM 外は ECMWF 降水確率/100 の期待値 | core.py L883-970 |
| **モデル取得・結合** `fetch_forecast()`: jma_msm → ecmwf_ifs025 の順で時刻ごとにマージ (MSM 優先)、visibility は `best_match` (model=None) で別取得。`precipitation_probability`/`cape` は ECMWF 由来 | `mountain_weather_mvp.py` L176-253、detail.py L258-292 も同型 |
| 日別ウィンドウ集計 `window_scores_by_day()`: activity/ridge/pm/day_peak (日の出-1h〜日没+30min 等) | mvp.py L256- |
| ECMWF アンサンブル確信度 `compute_ensemble_confidence_by_day()` (ENSEMBLE_MODEL=ecmwf_ifs025, 51メンバー、`abs(vote-50)*2`) | `mountain_weather_detail.py` L2173-2300 付近 |
| 地形レイヤー (GSI DEM、風上/風下) | `mountain_terrain.py`, `terrain_profiles.json` |
| キャッシュ (3h TTL) | core.py L1009-1040, `cache/` |
| 仕様・設計知見 | `skills/yamabiyori/SKILL.md`, `CHANGELOG.md` |
| **既存バックテストハーネス** | `tools/backtest/` (README.md, open_meteo_previous_runs.py, jma_amedas.py [data.jma.go.jp/stats/etrn スクレイパ], stations.py [6地点], metrics.py, run_backtest.py, run_to_run.py, bias_audit.py, multi_model_blend.py, pseudo_ensemble_dryday.py) |

既存ハーネス README の結論で今回の設計に直接効くもの (再検証はしていない、README の記載):
- Previous Runs で `precipitation_probability_previous_dayN` は全null (今回も確認)。
- JMA の過去統計 (`data.jma.go.jp/stats/etrn`, HTML) は数十年遡れるが**雲量・天気の欄は現在ほぼ空**。日照時間を雲の代理にした (閾値 0.4)。
- 6地点 (河口湖・軽井沢・奥日光・白馬・野辺山・菅平) で夏冬 45日ずつ検証済み: 気温バイアスは季節で符号反転、風は谷底立地の代表性問題が支配的、AIFS (`ecmwf_aifs025_single`) の風 MAE が最良。
- ECMWF ENS (ensemble-api) には lead 別アーカイブが無い。

**本体側で今回の検証に不利な点**: 本体が実際にユーザーに見せる値は「気圧面から山頂へ内挿した雲量」であり、Previous Runs API で遡れる「格子の全雲量」とは別物。両者の差は design.md §2 で扱う。

---

## 7. 未確認事項の一覧 (再掲)

1. `previous_dayN` が 4ラン/日のどのランを指すか。
2. `previous_day7` のアーカイブ開始日 (day1 より遅い可能性)。
3. 他地点でのアーカイブ開始日の一致。
4. Single Runs で 1000/925/900/800/600hPa (850/925/700 以外) の可否、18Z ランの可否。
5. 429 レート制限の実挙動、`X-RateLimit` 系ヘッダの有無 (今回は観測されず)。
6. CC BY 4.0 の具体的な帰属表示文言、キャッシュ/再配布ポリシー。
7. アメダス map の保持期間の正確な境界 (8日 OK / 10日 NG、9日は未試行)、JMA 側のアクセス制限。
8. アメダス `elems` の 5,6,8 文字目、`weather` コードの意味。
9. 76地点同時リクエストの可否 (3地点は OK)。

---

## 8. Phase 1 の一括取得で追加で分かったこと (2026-09-18 夜、5地点 × 2モデル × 2025-09-18〜2026-09-17)

- **アーカイブの穴**: 全5地点で共通に、`ecmwf_ifs025` は **2026-04-17 13:00Z〜04-22 23:00Z** の一部 (variable × lead) が null
  (cloud_cover d2/d3、precipitation d5/d7、relative_humidity d1 …計 5 組、各 645 行/5地点)。`jma_msm` は
  **2026-04-05 19:00Z〜04-10 05:00Z** (lead 1/2) と **2025-11-11 07:00〜11:00Z** (lead 2) が null。
  全体の欠損率は 1.2〜1.5% でこれらに集中しており、時刻の重複・欠落はゼロ。Open-Meteo 側の欠損とみられる (原因未確認)。
- **返却 `elevation` は格子標高ではなく要求地点付近の DEM 標高**: 唐松岳 (36.7595,137.7503) と八方池 (36.6942,137.7844) は
  `ecmwf_ifs025` で同じ格子 (36.75,137.75) に落ち、cloud_cover / precipitation / relative_humidity / wind_speed は
  **100% 同一値**だが `elevation` は 2550m / 2066m と異なり、**temperature_2m だけ平均 −3.15°C 差** (標高差 484m ×
  6.5°C/km = 3.1°C と一致)。→ Open-Meteo は気温のみ DEM 標高へ気温減率で補正し、他の変数は格子値そのまま。
  「唐松岳と八方池を分けて検証する」意味があるのは気温だけで、雲量・降水・風は同一格子の同一値になる (ECMWF 0.25° の場合)。
  `jma_msm` は格子が細かく (36.75 vs 36.65)、両地点で値が異なる。
- **10m 風速は小さい**: 5地点・全リード・1年分で最大 9.5 m/s、p99 4.2〜5.7 m/s。本体が表示する稜線風 (気圧面から内挿) とは
  別物なので、Previous Runs の `wind_speed_10m` で本体の風の精度は評価できない (Track B の仕事)。
- 気温の範囲は −34.3°C (富士山格子, 2026-02-08) 〜 23.2°C (八方池, 2026-07-15)。値域外の値なし。
- `jma_msm` の 2026-05 チャンク (唐松岳) だけ、サーバ側の `Unexpected error while streaming data: timeoutReached`
  (HTTP 200・非JSON本文) が 3 回続き、4 回目で成功 (gen 209ms)。他の 129 チャンクは初回で成功。
- Previous Runs で `relative_humidity_2m_previous_dayN` は両モデルとも取得できた (Phase 0 では未確認だった)。

---

## 9. Phase 1 レビューで追加確認したこと (2026-09-18 夜)

### 9.1 jma_msm の `previous_day3` は列数に関係なく全 null

3列だけ (`temperature_2m/cloud_cover/precipitation` の `_previous_day3`) と、別の組 (`temperature_2m_previous_day2` +
day3 の温度・風速) で、2026-08 と 2026-03 の各1ヶ月 (744h) を取得 (`probe/log_msm_lead3.txt`)。
タイムアウトは一度も起きず (generationtime 0〜1ms)、**day3 は 4 リクエスト × 744h 全て null、day2 は 744/744 全時刻に値あり**。
MSM の 00/12Z ランは 78 時間先まであるので「valid time − 72h のラン」は存在し得るが、Open-Meteo のアーカイブは
MSM について day1/day2 しか持っていない、と結論する。day2 が全 24 時刻で埋まっていることから、`previous_dayN` は
「valid time の N×24h 以上前に発行された最新ラン」(3時間ごとのランを使い分けている) と解釈するのが整合的 (定義自体は未確認のまま)。
→ Phase 1 の `jma_msm` はリード 1-2 のみ、で確定。

### 9.2 ECMWF 帯で本体がどう標高別雲量を出しているか (本体コード読み取り、変更なし)

- `mountain_weather_mvp.py` / `mountain_weather_detail.py` の `fetch_forecast()` は jma_msm と ecmwf_ifs025 に
  **同じ変数セット** (`HOURLY_VARS` = cloudcover, cloudcover_low/mid/high, precipitation_probability, precipitation, cape
  + `level_stack_vars()` = cloudcover/RH/風速/風向/気温/geopotential_height @1000,925,900,850,800,700,600hPa) を投げ、
  時刻ごとに MSM 優先でマージする (mvp.py L204-237)。
- **スコアに入る雲量は `cloudcover_at_summit`** = `core.add_altitude_columns()` が気圧面の `cloudcover_XXXhPa` を
  `geopotential_height_XXXhPa` を高さ軸にして山頂標高へ線形内挿したもの (core.py L306-339; mvp.py L330
  `cloud_series = forecast["hourly"][SUMMIT_VARS["cloudcover"]]`、detail.py L417/L603 も同じ)。
  MSM 帯も ECMWF 帯も同じコードパスで、ECMWF 帯は 900/800hPa が null なので 850↔700hPa 間の内挿になる (core.py の注記どおり)。
- **`cloudcover_low/mid/high` は取得しているがスコアには使っていない**。用途は detail.py L607-610 の時系列表示と、
  雲海判定 `detect_cloud_sea_opportunity()` の "surface" 候補 (これは全雲量 `cloudcover`、L1090) のみ。
- したがって「low/mid/high をそのまま標高帯の雲量として使っている」というバグは**無い**。
- ただし Open-Meteo の ECMWF ドキュメント (WebFetch で確認) によると:
  - 「Pressure-level cloud cover: Estimated from pressure-level relative humidity following Sundqvist et al. (1989)」
    → **本体が内挿元にしている `cloudcover_XXXhPa` は ECMWF ネイティブの雲量ではなく、Open-Meteo が気圧面 RH から
    近似した値**。
  - `cloud_cover_low/mid/high` も「In case of ECMWF IFS it is based on relative humidity on pressure levels 1000, 925
    and 850 hPa」(mid: 700/500、high: 300/250/200) → こちらも RH 由来。
  - ECMWF の全雲量 `cloud_cover` だけがネイティブ出力とみられる (ドキュメントに導出の記載なし。未確認)。
  - jma_msm については同ページに記載が無く、気圧面雲量がネイティブか RH 由来かは**未確認**。
  → 本体の ECMWF 帯の山頂雲量は「RH→雲量の経験式 (Sundqvist)」を通した値で、これはバグではなく Open-Meteo の仕様。
    Phase 3 (Track B) で検証すべきは、この RH 由来の山頂雲量が実況 (日照) に対してどれだけ当たるか、と、
    全雲量 (ネイティブ) との差。

### 9.3 lead5→6 で気温 MAE が 1.17→1.66 に跳ねる件

- ドキュメント記載 (ECMWF API ページ): 「The first 90 forecast hours use 1-hourly data, switching to 3-hourly after 90
  hours and 6 hourly after 144 hours」「The Open-Meteo API dynamically interpolates all data to a consistent 1-hourly
  time-series」。`previous_day6` は valid time の 144〜167h 前のラン → **全て 6 時間刻み区間の補間値**、day5 は
  120〜143h (3 時間刻み区間)。境界はちょうど lead5/6 の間にある。
- Single Runs (run 2026-09-01T00Z, forecast_days=16) の生系列で 144h 前後を見ても、値は滑らかで区分線形ではない
  (2階差分≈0 の割合は 0-90h 0.18 / 90-144h 0.29 / 144h+ 0.34-0.36)。Open-Meteo の補間は線形ではなく、
  生の刻みをレスポンスから直接は判別できない。
- Parquet で MAE を「00/06/12/18 UTC (6h 節点)」と「それ以外の時刻」に分けると:

  | lead | 節点時刻 MAE | 非節点 MAE |
  |---|---|---|
  | 2〜5 | 0.61 / 0.82 / 1.03 / 1.20 | 0.58 / 0.78 / 0.99 / 1.16 (節点の方が僅かに悪い) |
  | 6 | **1.53** | **1.69** |
  | 7 | **1.81** | **1.96** |

  lead 6 以降だけ「非節点時刻の方が悪い」に反転する — 補間による追加誤差の signature で、ドキュメントと整合する。
  ただし節点時刻でも 1.20→1.53 の段差は残るので、**刻み変化 (補間) で説明できるのは跳ねの一部 (〜0.15°C)**。
  残りは 6 日目の実際の精度低下、および `previous_dayN` がどのランを指すかの未確認の定義に依存する可能性がある。
  実況との比較 (Phase 2 以降) で改めて見る。

### 9.4 Phase 2 の初回実行で分かったこと

- Forecast API (api.open-meteo.com) の複数地点リクエストは list で返り、`timezone=UTC` だと系列が当日 00Z から始まる。
  **レスポンスにはランの初期時刻が無い**。ただし最後の非 null 時刻から推定できる: 2026-09-18 13:10Z 取得の
  jma_msm は 00Z〜09-21 06Z の 79h (= 00Z ラン +78h)、ecmwf_ifs025 は 363h (= 00Z ラン + 15日 + 3h)。
  つまり 13:10Z 時点では両モデルとも 00Z ランがまだ最新 (12Z ランは未反映)。スロット取得時刻とこの推定でリードを計算する。
- jma_msm の Forecast API では `precipitation_probability` / `wind_gusts_10m` / `cape` が全 null (本体の注記と一致)。
- アメダス map (毎正時) は 13 観測所 × 7 要素で 1 時間 ≈ 1.5KB (gzip 後の日次ファイルで ≈ 10KB/日)。

---

## 10. Phase 2 レビュー対応・Phase 3 (Track B) で確認したこと (2026-09-18 深夜)

### 10.1 Open-Meteo メタデータ API

- **エンドポイント (実測で確定)**: `https://api.open-meteo.com/data/<model>/static/meta.json`
  (`ecmwf_ifs025`, `jma_msm`, `ecmwf_ifs` で HTTP 200)。`.../data/<model>/meta.json` は 500、`/v1/meta?models=` は 404。
- 返るキー (実測): `last_run_initialisation_time`, `last_run_modification_time`, `last_run_availability_time`,
  `temporal_resolution_seconds`, `update_interval_seconds`, `data_end_time` (いずれも UNIX 秒), `chunk_time_length`, `crs_wkt`。
- 2026-09-18 12:4x UTC の値: ecmwf_ifs025 = 初期時刻 **00Z**、修正 07:40:50Z、公開 07:43:53Z、**temporal_resolution 10800s (3h)**、
  update_interval 21600s、data_end 2026-10-03 03Z。jma_msm = 初期時刻 **09Z**、修正 12:29:57Z、公開 12:32:31Z、
  temporal_resolution 3600s、update_interval 10800s、data_end 2026-09-20 01Z (= 09Z + 40h)。
- **Forecast API のレスポンスは複数ランの継ぎ足し**: 同時刻の jma_msm スナップショット (§9.4) は 00Z から 79h 非 null
  (= 00Z ランの 78h ホライズン) だったが、メタデータの最新ランは 09Z (39h ラン, data_end 09-20 01Z)。
  つまり 09Z ランで上書きされる範囲より先の時刻は古い 00Z ランの値がそのまま残っている。
  「1 スナップショット = 1 ラン」ではないので、Forecast API スナップショットからのリード計算は近似にしかならない。
  時間単位で厳密なリードは Single Runs (Track B) で扱う。
- 収集側の対応: スナップショット直前・直後にメタデータを `<HH>Z_meta_before/after.json.gz` に保存。
  `last_run_modification_time` から 10 分未満なら待つ。cron (`20 */3`) は ECMWF 00/06/12/18Z の公開 (+約7.7h) と
  MSM 3 時間ごとの公開 (+約3.5h) に対し 10 分以上離れているが、遅延が重なった場合は上記の待機で吸収する。

### 10.2 富士山 (50066) の観測要素の再確認

- 収集済み 8 日分 (2026-09-11 00:00〜09-18 22:00 JST、191 正時) の `sun1h` は **190 正時が `[null, 5]`、1 正時が `[null, 6]`**。
  他の高標高地点 (野辺山・奥日光・白馬) は同時刻に `[0.0, 0]` 等の値がある → 抽出側の問題ではない。
- `amedastable.json` の 富士山 `elems` = `10001011`。実測で対応が取れている 4 文字目 (日照) が **0** = 日照は観測要素に含まれていない。
  少なくとも 2026-09 の 8 日間は恒常的に無観測で、機器障害か観測終了かはこの窓では判別できない (未確認)。
- 一方 **湿度と気圧は取れる**: 正時の map JSON と point JSON に `humidity` (例 100%) と `pressure` (例 653.4hPa) がある。
  10 分値 (正時以外) では両方 null。→ 収集要素に `pressure` を追加し、8 日分を再収集した (旧ファイルは
  `data/raw_superseded/obs_amedas_without_pressure/`)。
- 風は map/point とも要素自体が無い (2004-08 の常駐終了時に風向風速・全天日射の観測を終了、というご指摘と整合)。

### 10.3 Single Runs API の運用上の性質 (Track B のフェッチで実測)

- **5 地点同時 × 21 列 (RH/GPH/雲量 @1000/925/850/700/600hPa + 地上 6 変数)** で 1 リクエスト
  ≈ 60〜350KB、初回は **20〜230 秒** (generationtime 5〜90 秒)。同じランを再要求すると数 ms〜数秒。
  サーバ側の cold read で HTTP 500 や HTTP 200 + 非 JSON 本文 `Unexpected error while streaming data: timeoutReached`
  が出るが、時間をおいて再試行すると成功する。
- **`jma_msm` は Single Runs では 900/800hPa が全 null** (両モデルとも)。通常 Forecast API では jma_msm が
  1000〜600 の全面を返す (本体 core.py の注記) のと異なる。→ Track B の再現は 1000/925/850/700/600 の 5 面で行う
  (本体が MSM 帯で使う 900/800 面は再現できない = Track B の MSM 帯山頂雲量は本体の値と一致しない可能性がある)。
- **存在しないラン**は `run=` 指定が 400 で返る場合と、HTTP 200 + 非 JSON `...modelRunUnavailable(model: ..., run: ...)`
  で返る場合がある (2026-06-11T00Z は両モデルとも後者、2026-06-11T12Z / 06-12T00Z は存在)。フェッチャは両方を
  「ラン無し」として `.missing` マーカーを残す。
- 取得ペース: 2 モデル並行で **約 1 ラン/2〜5 分/モデル**。2026-06-11〜09-17 の 00Z (99 ラン × 2) は数時間かかる。
  フェッチは再開可能 (`python -m backtest.fetch_single_runs --start ... --end ... --hours 0`)。

### 10.4 既存ハーネスの観測キャッシュ (読み取りのみ)

`C:/mountain-weather/tools/backtest/cache/amedas/<prec>_<block>/<date>.json` に JMA etrn (過去統計) 由来の毎時値がある:
6 地点 (白馬 48_396・野辺山 48_415・奥日光 41_47690・菅平 48_992・軽井沢 48_47622・河口湖 49_47640)、
2026-01-15〜02-28 と 2026-06-11〜09-14、要素 temp/dewpoint/humidity/precip/wind/sun_hr (+官署は気圧等)。
`obs_import.py` がこれを `observation_long.parquet` に取り込む (source=etrn_cache)。時刻は「hour N = N:00 JST に終わる 1 時間」、
hour 24 は翌日 00:00 JST として扱った。
