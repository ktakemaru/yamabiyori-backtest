# yamabiyori-backtest 設計 (Phase 0 案 → Phase 1 で一部確定)

前提事実は `docs/api-findings.md`。この文書は「何をどう作るか」だけを書く。
**判断の根拠が実測でないものは (判断) と印を付ける。**

## 0. 目的と評価軸

- 「N日前の予報は当たっていたか」をリードタイム別に定量化し、将来の MOS 補正の土台にする。
- 最重視は **雲量**、次いで **風**。降水・気温はその次。
- 本体 (`C:\mountain-weather`、読み取り専用) は一切変更しない。本リポジトリは独立。

## 1. 2本立てのデータ経路 (api-findings §1, §2 の帰結)

| | Track A: Previous Runs API | Track B: Single Runs API |
|---|---|---|
| 取れるもの | 地上変数 (全雲量・降水・気温・風速) の `previous_day1..7` | 各ランのフルホライズン。**気圧面 (雲量/RH/風/気温/GPH) が取れる** = 本体の山頂内挿を再現できる |
| 取れないもの | cloud_cover_low/mid/high、気圧面、突風、確率、CAPE (全て null か 400) | ecmwf_ifs (9km) の気圧面は null。ECMWF に precipitation_probability を混ぜると 400 |
| 期間 | ecmwf_ifs025 2024-02-04〜、jma_msm 2022-07-02〜 (day2 まで) | ecmwf_ifs025 2026-04-02〜、jma_msm 2026-05-13〜、ecmwf_ifs 2024-03-14〜 (地上のみ) |
| リード | 日単位 (「約N日前」。どのランかは未確認) | ランの初期時刻から時間単位で厳密 |
| 用途 | **Phase 1**: 長期 (12ヶ月+) のリード別スキル、季節性、MOS の学習データ | Phase 3: 8日以上先、run-to-run 一貫性、本体スコア (山頂内挿) の再現検証 |

(判断) Phase 1 は Track A に限定する。本体が実際に表示する「山頂内挿雲量」はここでは検証できず、**格子の全雲量** (格子標高 ≈2550m@唐松岳) の検証になる。この差は Phase 3 で Track B を使って埋める。

## 2. データスキーマ (long 形式)

Parquet 1テーブル `forecast_long`:

| 列 | 型 | 説明 |
|---|---|---|
| site_id | str | 検証地点 ID (例 `karamatsu`) |
| model | str | `jma_msm` / `ecmwf_ifs025` |
| variable | str | `cloud_cover`, `precipitation`, `temperature_2m`, `wind_speed_10m`, … (Open-Meteo の変数名そのまま) |
| lead_days | int8 | 1..7 (`previous_dayN` の N)。0 = 最新ラン (Phase 1 では取得しない) |
| valid_time | datetime (UTC) | 対象時刻。JST は解析時に +9h |
| value | float32 | 予報値。単位は `unit` 列 (風速は `wind_speed_unit=ms` で m/s に統一) |
| unit | str | レスポンスの `hourly_units` |
| grid_lat / grid_lon / grid_elevation_m | float | Open-Meteo が返した格子点座標と格子標高 (要求座標とは別に保持) |
| fetched_at | datetime (UTC) | 取得時刻 (再取得時の差分検出用) |

観測 (Phase 2) は `observation_long`: `station_id, valid_time, element (temp/wind/sun1h/precipitation1h/…), value, quality_flag`。

評価時に `site ↔ station` の対応表 (§4) で結合し、`(site, model, variable, lead_days, valid_time)` ごとに forecast/obs のペアを作る。

理由 (判断): wide 形式だとモデル×リード×変数で列が爆発し、リードや変数の追加が全てスキーマ変更になる。long なら追加は行の追加で済み、欠損率も `group_by` で一様に出せる。

## 3. ディレクトリ構成とストレージ

```
C:\yamabiyori-backtest\
  backtest/                  # パッケージ
    config.py                # 地点・モデル・変数・リード・期間
    openmeteo_previous_runs.py  # 取得 (生JSON保存まで)
    parse.py                 # 生JSON -> long DataFrame
    fetch_previous_runs.py   # __main__: 取得→パース→Parquet→サマリ
  data/
    raw/previous_runs/<model>/<site_id>/<YYYY-MM>.json.gz   # 生レスポンス (加工前、不変、gzip)
    raw_superseded/            # 地点差し替え等で使わなくなった生レスポンス (証拠として保持)
    parquet/forecast_long.parquet
    obs/amedas/<JST日>.json.gz                     # Phase 2 系統1
    snapshots/forecast/<UTC日>/<HH>Z_<model>.json.gz  # Phase 2 系統2
  .github/workflows/collect.yml
  docs/                      # api-findings.md, design.md
  probe/                     # Phase 0 の探索スクリプトと生レスポンス (証拠として保持)
  tests/                     # フィクスチャは保存した実レスポンス
```

- **生 JSON を必ず残し、パースと分離**する。パースを直しても再取得しない。
- 取得単位は `(model, site, 月)`。ファイルが存在すれば取得済みとみなしスキップ (再開可能)。壊れた JSON は削除して再取得。
- Parquet ライブラリは **polars** (pyarrow 不要でネイティブに Parquet を読み書きできる)。Python は 3.13 系。
- `data/raw` (gzip)・`data/raw_superseded`・`data/parquet`・`data/obs`・`data/snapshots` は **git 管理する**
  (Phase 1 レビューの指示: サーバ側の欠損が将来変わる可能性があるため、取得できた内容を証拠として残す)。
  `probe/raw` と `tests/fixtures` も管理する。

## 4. 検証地点の選び方

原則: **稜線/山頂の格子点** と **最寄りの麓アメダス** をペアで定義し、両方を保持する。ただし api-findings §5.2 の通り、温度・風・日照が揃う地点は野辺山 (1350m) が最高で、稜線に観測点は富士山しか無い。

(判断) 検証 5 地点 (Phase 1 レビューで八方池 → 安達太良山に差し替え):

| site_id | 地点 | lat, lon | 標高 | ペア観測所 (Phase 2) | 選定理由 |
|---|---|---|---|---|---|
| karamatsu | 唐松岳 | 36.7595, 137.7503 | 2696m | 白馬 48141 (703m, 12km) | 指定。本体の基準山 |
| fuji | 富士山(剣ヶ峰) | 35.3606, 138.7274 | 3776m | 富士山 50066 (3775m; 気温・湿度・気圧) | **唯一の山頂実況**。気温と、**気圧面 RH の実測値として湿度** (Track B ①) を検証できる。日照は 2026-09 の 8 日間全て欠測 (api-findings §10.2)。**風は 2004-08 の常駐終了時に観測終了 → 山頂の風の実況は存在しない。風の検証地点としては期待しない** |
| akadake | 八ヶ岳(赤岳) | 35.9722, 138.3672 | 2899m | 野辺山 48571 (1350m, フル要素) | フル要素で最も高い観測所 |
| nikko_shirane | 日光白根山 | 36.7983, 139.3742 | 2578m | 奥日光 41166 (1292m, 官署: 視程・天気あり) | 官署クラス、雲量代理を日照以外でも検討できる |
| adatara | 安達太良山 | 37.6256, 140.2864 | 1700m | 鷲倉 36196 (1220m, 5.3km, フル要素) | 全76座で最寄りフル要素観測所との距離・標高差 (480m) が最小。東北の格子 |

八方池 (36.6942,137.7844) は ECMWF 0.25° で唐松岳と同一格子に落ち、気温以外は同一値になる (api-findings §8) ため外した。
座標は本体 `MOUNTAINS` の値。今後 76 座全てへ広げる際は `MOUNTAINS` を読み込んで生成する (本体は読み取りのみ)。

Phase 2 で収集するアメダス観測所 (`config.AMEDAS_STATIONS`, 13 地点): 上記ペア 5 + 高標高の参考地点
(菅平 1253m・田代 1230m・草津 1223m・開田高原 1130m・奈川 1068m・軽井沢 999m・山中 992m) + 官署 長野。

## 5. 評価指標

- 連続値 (気温・風速・全雲量%): **MAE, bias (forecast−obs), RMSE**、リード別・季節別・地点別。風は bias% (bias / 観測平均) も出す (既存ハーネスで谷底立地の代表性問題を切り分けるのに効いた)。
- 雲量 → 2値: 観測側は `sun1h` から「晴れ (sun1h ≥ 閾値)」、予報側は `cloud_cover ≤ 閾値` (判断: 閾値は 0.4h / 50% を初期値にし、Phase 2 で ROC を見て決める。**昼間のみ**、日の出/日の入りは地点ごとに計算)。
- 降水 → 2値: 観測 `precipitation1h ≥ 0.5mm`、予報 `precipitation ≥ 0.1mm` (判断、要調整)。
- 2値の指標: 適中率 (accuracy)、空振り率 (false alarm ratio)、見逃し率 (miss rate)、ヒット率 (POD)、**ベースライン** (常に晴れ/常に乾き) と比較。
- **確信度の較正** (Phase 3): 本体の確信度スコア vs 実際の適中率。
- 各集計に **サンプル数 n** を必ず併記する。

## 6. フェーズ分割

| Phase | 内容 | 出力 |
|---|---|---|
| 0 (済) | API 実測、本体構造把握、設計 | api-findings.md, design.md |
| **1** | Previous Runs から 5 地点 × 12 ヶ月 × 2 モデルを一括取得 → Parquet。再実行可能、生JSON保存、単体テスト | `data/parquet/forecast_long.parquet`, サマリ |
| **2** (実装済) | GitHub Actions で 3 時間ごとに (a) アメダス毎正時 map から 13 観測所を抽出 → `data/obs/amedas/<JST日>.json.gz`、(b) Forecast API の 16 日予報スナップショット (層別雲量入り、6h スロット) → `data/snapshots/forecast/<UTC日>/<HH>Z_<model>.json.gz`。`check_collection.py` で欠損確認 | 生 JSON (パースは Phase 3) |
| **3** (進行中) | Track B: Single Runs で気圧面 (RH/GPH/雲量) を取得 (`fetch_single_runs.py`, 00Z, 2026-06-11〜)、本体の内挿ロジックを移植して**同一入力で同一出力を確認** (`summit_interp.py`, `tests/test_summit_interp.py`)、`observation_long` (アメダス map + 既存 etrn キャッシュ)、①/②③ の分離評価 (`trackb_eval.py`) → `docs/track-b-findings.md` | track-b-findings.md |
| 4 | MOS: 季節 (月) と地点を特徴量にした線形補正。まず気温、次に雲量 2値の閾値較正 | 補正係数テーブル |
| 5 | 本体への還元案 (本体は別途手動で反映、このリポジトリからは push しない) | 提案文書 |

## 7. リクエスト予算 (判断)

Phase 1: 2 モデル × 5 地点 × 12 ヶ月 = 120 リクエスト、1.5 秒間隔。1 リクエスト ≈ 月 744h × (変数×リード) 値。terms の無料枠 (10,000/日, 600/分) に対して十分小さい。76 座に広げても 1,824 リクエスト/年分。

## 8. Phase 2 の運用設計 (実装済)

- 1 ワークフロー `.github/workflows/collect.yml`、cron `20 */3 * * *` (UTC)。cron 遅延に対して:
  - アメダスは「保持窓 (190h) 内の未取得正時を全部取る」ので、遅れても・数回失敗しても次回で埋まる。新しい時刻から取る。
  - スナップショットは「実行時刻 −3h を含む 6h スロット」を取る。遅延が 3h 未満ならスロットは変わらない。取り損ねは事後に取れない。
- 二重取得なし: 既存の正時 / スロットファイルはスキップ。
- 負荷: JMA ≈ 8 リクエスト/日 (バックフィル時は最大 120/回)、Open-Meteo 8 リクエスト/日 (1 リクエストに 5 地点)。
- データコミットが 3 時間ごとに発生 → public リポの 60 日無コミット停止に当たらない。
- 毎日 00:50 UTC に `check_collection.py --days 7` を別ジョブで実行 (欠損があればジョブが赤くなる)。
- Forecast API のレスポンスにはラン初期時刻が無い。`fetched_at` と最後の非 null 時刻 (MSM 78h/39h、ECMWF 15日+3h) から
  推定する (api-findings §9.4)。6h スロットだと MSM の 8 ラン/日のうち半分しか捉えない — MSM のリード別評価は
  Single Runs (Phase 3) を主に使い、スナップショットは層別雲量のためと割り切る (判断)。
