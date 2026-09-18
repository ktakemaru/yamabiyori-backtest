# yamabiyori-backtest

[ヤマビヨリ](https://github.com/ktakemaru/yamabiyori) (登山特化型の天気予報ツール) の予報精度を
リードタイム別に検証し、将来のバイアス補正 (MOS) につなげるためのバックテスト基盤。
プラグイン本体とは独立したリポジトリで、本体のコードは読み取りのみ・一切変更しない。

- 調査結果 (実測に基づく事実): [docs/api-findings.md](docs/api-findings.md)
- 設計: [docs/design.md](docs/design.md)

## 状態

| Phase | 内容 | 状態 |
|---|---|---|
| 0 | API 実測・本体構造把握・設計 | 済 |
| 1 | Previous Runs API から 5 地点 × 2 モデル × 過去 12 ヶ月 (リード 1〜7 日) を一括取得 → Parquet | 済 (`data/parquet/forecast_long.parquet`) |
| 2 | GitHub Actions で日次収集: アメダス実況 + Forecast API スナップショット (層別雲量) | 実装済、稼働は GitHub 側で有効化が必要 (下記) |
| 3 | Track B: Single Runs の気圧面から本体の山頂雲量を再現し、① RH 予報誤差 / ②③ 診断+内挿誤差 を分離 | 進行中 (00Z ラン約4割取得、[docs/track-b-findings.md](docs/track-b-findings.md)) |

## セットアップ (Windows, Python 3.13 で確認)

```powershell
cd C:\yamabiyori-backtest
py -3.13 -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt
```

依存: `requests`, `polars`, `tzdata`, `pytest`、`numpy` (本体 `mountain_terrain.py` の import に必要)。pyarrow / pandas は使わない (polars がネイティブで Parquet を扱う)。
収集スクリプト (`collect_*`, `check_collection`) は `requests` だけで動く (`plugin_confidence_snapshot` だけ本体経由で `numpy` も要る)。

## Phase 1: Previous Runs の一括取得

```powershell
# 5地点 × 2モデル × 過去12ヶ月 (昨日を終端に364日)。約130リクエスト、1.5秒間隔
.\venv\Scripts\python.exe -m backtest.fetch_previous_runs

# 期間・地点・モデルを絞る
.\venv\Scripts\python.exe -m backtest.fetch_previous_runs --start 2026-06-01 --end 2026-06-30 --sites karamatsu fuji --models ecmwf_ifs025

# 取得せず、保存済みの生JSONから Parquet を作り直す (パースを直したとき)
.\venv\Scripts\python.exe -m backtest.fetch_previous_runs --skip-fetch

# Parquet の値域・時刻・簡易 MAE を確認
.\venv\Scripts\python.exe -m backtest.quicklook
```

- **再実行可能**: `data/raw/previous_runs/<model>/<site_id>/<YYYY-MM>.json.gz` が完全なチャンクはスキップする。
  途中で失敗しても同じコマンドを再実行すれば未取得分だけ取りに行く。API エラーは `*.error.json.gz` に残る。
- 1チャンク失敗しても全体は止めない。最後に失敗一覧を出し、終了コード 1 を返す。
- HTTP 429/5xx、タイムアウト、**HTTP 200 で本文が非JSON** (`Unexpected error while streaming data:
  timeoutReached` を実際に観測) は指数バックオフで最大 5 回再試行する。
- 終了時に Parquet の行数・期間・欠損率 (モデル × 変数 × リード) のサマリを標準出力に出す。
- `jma_msm` はリード 1〜2 だけ要求する (3 以降は Open-Meteo のアーカイブに無い。api-findings §9.1 で列数を絞って再確認済み)。
- 要求変数のうち `cloud_cover_low/mid/high` と `wind_gusts_10m` の `previous_dayN` は全 null (api-findings §1.1)。
  null 行として保持し、サマリの欠損率で見えるようにしている。

出力: `data/parquet/forecast_long.parquet` (スキーマは design.md §2、`valid_time` は **UTC**、風速は **m/s**)。

## Phase 2: 日次収集 (GitHub Actions)

`.github/workflows/collect.yml` が 3 時間ごと (`20 */3 * * *` UTC) に 3 系統を収集してコミットする。

| 系統 | 取得元 | 保存先 | 補足 |
|---|---|---|---|
| 1 アメダス実況 | `jma.go.jp/bosai/amedas/data/map/<正時>.json` | `data/obs/amedas/<JST日>.json.gz` | 13 観測所 × 7 要素 (`config.AMEDAS_STATIONS/ELEMENTS`) だけ抽出。約 8 日で 404 になるので、毎回「保持窓内の未取得正時」を全部取る (失敗しても次回で埋まる) |
| 2 予報スナップショット | `api.open-meteo.com/v1/forecast` | `data/snapshots/forecast/<UTC日>/<HH>Z_<model>.json.gz` | 5 地点 × 2 モデル、16 日先、`cloud_cover_low/mid/high` 入り、生レスポンスのまま。6 時間スロット (実行時刻 −3h を含む境界) |
| 3a アンサンブル全メンバー | `ensemble-api.open-meteo.com/v1/ensemble` (ecmwf_ifs025, 50+1 列) | `data/snapshots/ensemble/<ラン日>/<HH>Z_ecmwf_ifs025_members.json.gz` | **00Z ランのみ (日次)**。5 地点 × 22 変数 (雲量 全/低/中/高、降水、気温、925/850/700/600hPa の雲量/RH/気温/GPH) × 3 時間刻み × 16 日、生値のまま (スプレッドは後で計算)。約 0.7MB/日 |
| 3b 本体の確信度スコア | 本体 (`ktakemaru/yamabiyori` main) をライブラリとして import | `data/snapshots/plugin_confidence/<ラン日>/<HH>Z.json.gz` | 本体の `compute_ensemble_confidence_by_day()` を 5 座について本体と同じ引数で呼び、**本体の git hash** と入力生レスポンス・出力を保存。本体は読み取り専用 (`dont_write_bytecode`、`fetch_ensemble` をプロセス内でキャッシュ無し版に差し替え) |

ローカルでも同じコマンドで動く:

```powershell
.\venv\Scripts\python.exe -m backtest.collect_amedas              # 保持窓内の未取得正時を取る
.\venv\Scripts\python.exe -m backtest.collect_forecast_snapshot   # 現スロットのスナップショット
.\venv\Scripts\python.exe -m backtest.collect_ensemble             # 最新ランが 00Z で未保存なら取る (--run-hours 0,12 等で変更)
.\venv\Scripts\python.exe -m backtest.plugin_confidence_snapshot   # 本体 (既定 C:\mountain-weather、YAMABIYORI_PLUGIN_DIR で変更) の確信度を記録
.\venv\Scripts\python.exe -m backtest.check_collection --days 7   # 欠損チェック (欠損があれば終了コード 1)
```

系統3 の背景 (アンサンブルはリード別に遡れないので今から貯めるしかない) と、無料枠に対するリクエスト換算は
api-findings §11。Actions では本体を `plugin/` に読み取り専用で checkout する (`.gitignore` 済み、push しない)。

#### 系統3 のリクエスト換算と縮小案

Open-Meteo の無料枠 (600/分, 5,000/時, 10,000/日, IP ごと・固定窓) は **member 列を変数として数える**
(`weight = Σ_地点 max(1, 変数数 × 51 / 10 × 日数 / 14)`; サーバ実装と 429 の実測で確認, api-findings §11.5)。
現在の 1 日の合計は ≈ 870 call (系統2 146 + 3a 641 + 3b 82) で日次上限の 9%。ただし 3a の members リクエスト 1 本 (641) が
分当たり上限 600 を超えるため、直後の ensemble-api リクエストは 429 になる → 両スクリプトは 429 で 65 秒待って再試行する。

上限に近づいた場合 (共有ランナー IP で 429 が続く、系統を増やす等) は次の順で縮小する。式から効果を先に出しておく:

| 縮小案 | 3a の call | 備考 |
|---|---|---|
| 現行: 5 地点 × 22 変数 × 16 日 | 641 | 1 本で分上限超え (直後 429) |
| 日数 16 → 14 (`FORECAST_DAYS`) | 561 | 15〜16 日目は ENS でも AUC ≈ 0.55 (findings §4.4) なので失うものは少ない。**分上限内に収まる** |
| 変数 22 → 14 (`LEVELS_HPA` の temperature / geopotential_height を落とす) | 408 | 山頂内挿の GPH が無くなる (標準大気で代用可)。温度スプレッドは本体の temp_confidence 用なので 3b 側で残る |
| 地点 5 → 3 (`config.SITES` を 唐松岳・富士山・八ヶ岳 に) | 385 | 日光白根 (山頂実況なし) と安達太良 (2026-09-10 以降しか実況なし) を落とす |
| 日数 14 + 変数 14 | 357 | |
| 地点 3 + 変数 14 + 日数 14 | 214 | 系統2 と合わせても 1 分窓で 429 にならない |

変更手順: `backtest/collect_ensemble.py` の `FORECAST_DAYS` / `LEVEL_VARS` / `HOURLY_VARS`、`backtest/config.py` の `SITES`。
`tests/test_ensemble.py::test_build_params_native_3h_and_vars` を合わせて直す。保存済みファイルはそのまま (envelope に `request` が入っている
ので後から何を取ったか分かる)。

### GitHub 側で必要な設定

1. リポジトリを GitHub (public 想定) に push する。
2. Settings → Actions → General → Workflow permissions を **Read and write permissions** にする
   (ワークフローが `data/` をコミット・push するため。`permissions: contents: write` も宣言済み)。
3. Actions タブで `collect` ワークフローを一度 **Run workflow** (workflow_dispatch) して動作確認する。
4. 1 週間後に Actions の `check` ジョブ (毎日 00:50 UTC) が緑か、またはローカルで
   `python -m backtest.check_collection --days 7` を実行して欠損が無いことを確認する。

設計上の注意 (design.md §8): cron の遅延は 3 時間まで吸収する。データコミットが 3 時間ごとに発生するので、public
リポジトリの「60 日間コミット無しで schedule 停止」には当たらない。同一時刻・同一スロットの二重取得はしない。
Forecast API のレスポンスにはランの初期時刻が無いので、`fetched_at` と最後の非 null 時刻から推定する (api-findings §9.4)。

## Phase 3: Track B (Single Runs + 気圧面)

```powershell
# 気圧面 (RH/GPH/雲量 @1000/925/850/700/600hPa) を 1 ラン × 5 地点で取得。再開可能。初回アクセスは 20〜230 秒/ラン
.\venv\Scripts\python.exe -m backtest.fetch_single_runs --start 2026-06-11 --end 2026-09-17 --hours 0            # 00Z
.\venv\Scripts\python.exe -m backtest.fetch_single_runs --start 2026-06-11 --end 2026-09-17 --hours 0 12 --models ecmwf_ifs025
.\venv\Scripts\python.exe -m backtest.parse_single_runs     # → data/parquet/single_runs_long.parquet (本体と同じ内挿の派生列付き)
.\venv\Scripts\python.exe -m backtest.obs_import            # → data/parquet/observation_long.parquet (アメダス map + 既存 etrn キャッシュ)
.\venv\Scripts\python.exe -m backtest.trackb_eval --out docs/track-b-tables.txt
```

- `backtest/jma_etrn.py --station fuji --start ... --end ...`: 気象庁「過去の気象データ検索」から富士山官署の時別値
  (夏季のみ日照あり, api-findings §10.5) を取得 (1 日 1 ページ, 2 秒間隔)。`obs_import` が取り込む。
- `backtest/skill.py`: ROC AUC (DeLong SE 付き) と Peirce skill score。`trackb_eval` の §C で閾値に依存しない識別性能を出す。
- `backtest/summit_interp.py` は本体 core.py L293-339 の内挿の移植。`tests/test_summit_interp.py` が本体を import
  (読み取りのみ) して同一入力・同一出力を確認する (本体が無い環境ではスキップ)。
- `backtest/tracka_eval.py`: Track A (Previous Runs, 通年) の地上全雲量で季節依存を確認 → `docs/track-a-season-tables.txt`。
- `backtest/precip_threshold.py`: 降水閾値 0.1mm/h の実害 (予報 ≥0.1 のうち実況 <0.5mm の割合、0.1 張り付き、閾値候補の分割表)
  → `docs/precip-threshold-tables.txt` (product-recommendations R8)。
- `backtest/quantile_map.py`: 山頂雲量 → 晴れる確率の分位点マッピング (0% 塊は経験確率 1 点 + 正値は地点別 CDF の分位ビン) を
  leave-one-site-out で Brier / 信頼度図により評価 → `docs/quantile-map-tables.txt` (findings §9)。暖候期のみ・本体には入れない。
- 結果と解釈は [docs/track-b-findings.md](docs/track-b-findings.md)、本体への推奨は [docs/product-recommendations.md](docs/product-recommendations.md)。

## テスト

```powershell
.\venv\Scripts\python.exe -m pytest -q
```

ネットワークには出ない。フィクスチャ `tests/fixtures/` は全て**実レスポンス** (Previous Runs 2 日分、アメダス全国マップ 1 時刻、
Forecast スナップショット 1 つ)。

## 地点

| site_id | 地点 | 標高 | ペア観測所 |
|---|---|---|---|
| karamatsu | 唐松岳 | 2696m | 白馬 48141 (703m) |
| fuji | 富士山(剣ヶ峰) | 3776m | 富士山 50066 (3775m、唯一の山頂観測: 気温・湿度・気圧。日照は欠測、風は 2004 年に観測終了) |
| akadake | 八ヶ岳(赤岳) | 2899m | 野辺山 48571 (1350m) |
| nikko_shirane | 日光白根山 | 2578m | 奥日光 41166 (1292m、官署) |
| adatara | 安達太良山 | 1700m | 鷲倉 36196 (1220m, 5.3km) |

八方池は ECMWF 0.25° で唐松岳と同一格子だったため外した (生データは `data/raw_superseded/` に保持)。

## データの出典

- 予報: [Open-Meteo](https://open-meteo.com/) (CC BY 4.0)。非商用・無料枠の範囲で利用。
- 観測: 出典: 気象庁ホームページ (https://www.jma.go.jp/bosai/amedas/)。
