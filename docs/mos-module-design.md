# MOS モジュール設計 (段階1: 調査と設計 → 段階2: 実装)

作成 2026-09-23 (段階1)、同日 段階2 のレビュー回答 (Q1〜Q3・追加指示) を反映。対象: R1 (山頂雲量 → P(晴れ) の較正表) を、山固有の処理から切り離した移植可能なモジュール `mos/` にする。
本書の「事実」はコード・git・データを読んで確認したものだけ。確認できなかったものは **未確認** と書く。

---

## 1. 調査結果 (事実)

### 1.1 本体の R1 実装箇所

**版の特定**

- 本体 `C:\mountain-weather` の作業ツリーは現在 **`feature/backtest-harness` (a65656e)** を checkout 中で、ここには R1 が **無い**
  (`mountain_weather_core.py` に `CLOUD_CALIBRATION_TABLE` が 0 件。`git diff main` で core が 190 行少ない)。
- v1.5.0 は `main` ブランチ (= `origin/main`、ローカルで `52d3d2a`)。タグは無い。`plugin.json` の `"version": "1.5.0"` は 65c6e75 で入った。
- R1 関連コミット (main): `acc98c7` WIP → `65c6e75` v1.5.0 (案C + 2900〜3300m ランプ) → `53e4e3b` (全山頂に適用、`MIN_SCORE_THRESHOLD` 80→70)。
  `mountain_weather_core.py` は **53e4e3b 以降 main HEAD (52d3d2a) まで無変更** (`git log main -- mountain_weather_core.py`)。
- `C:\mountain-weather-r8` は同じリポジトリの **git worktree** で `main` (52d3d2a) を checkout している (`git worktree list`)。
- origin/main がリモートでさらに進んでいるかは **未確認** (fetch していない)。

**表と適用処理 (すべて `mountain_weather_core.py` @ main, 行番号は 52d3d2a)**

| 種類 | 名前 | 行 | 内容 |
|---|---|---|---|
| 説明バナー | — | 520〜599 | 根拠・案A/B/C・適用範囲・注意書き |
| 定数 | `CLOUD_CALIBRATION_ENABLED` | 601 | `True`。A/B スイッチ |
| 定数 | `CLOUD_CALIBRATION_FULL_SUMMIT_M` | 602 | `None` (= 全山頂を完全較正) |
| 定数 | `CLOUD_CALIBRATION_RAW_SUMMIT_M` | 603 | `3300.0` (FULL が None の間は不使用) |
| 定数 | `CLOUD_CALIBRATION_BIN_EDGES` | 604 | `[0, 10, 20, 30, 40, 50, 70, 100]` |
| 表 | `CLOUD_CALIBRATION_TABLE` | 607〜613 | `(model, lead_lo, lead_hi, [P(晴れ) × 8 ビン])` の 5 行。コードに直書き |
| 定数 | `SUMMIT_CLOUD_CAL_VAR` | 614 | `"cloudcover_at_summit_cal"` |
| 定数 | `CLOUD_CALIBRATION_P_REF` | 616 | モデルごとに `lead_lo == 1` の行の第 0 ビン (案C の基準 P) |
| 関数 | `cloud_calibration_bin(cloud_pct)` | 619 | `<=0` → 0、以降 `(edge[i-1], edge[i]]`、100 超は最終ビン |
| 関数 | `cloud_calibration_row(model, lead_day)` | 628 | 未知モデル → `ecmwf_ifs025` の行。lead 範囲外 → 最も近い群 (同距離は表の先の行) |
| 関数 | `calibrated_cloud_pct(cloud_pct, model, lead_day)` | 637 | `round(min(100, max(0, 100*(1 - p/p_ref))), 1)`。`None` は素通し。p_ref が未知モデル (または 0) なら ecmwf の p_ref |
| 関数 | `cloud_calibration_weight(summit_m)` | 647 | 標高ランプ。現設定では常に 1.0 (summit_m が None または ENABLED=False なら 0.0) |
| 関数 | `lead_day_for(t_iso, today)` | 658 | `max(1, (日付 − today).days + 1)`。today = 系列の先頭日 (既定) |
| 関数 | `calibrated_summit_cloud_series(hourly, summit_m, today=None)` | 664 | 毎時: `precipitation_msm` が非 null → `jma_msm`、それ以外 → `ecmwf_ifs025`。`w*cal + (1-w)*raw` を round 1 桁 |
| 関数 | `cloud_penalty(cloud_pct)` | 690 付近 | 受け取った (較正済み) 雲量をそのままペナルティに |

**呼び出し元** (main): `mountain_weather_mvp.py:247`、`mountain_weather_detail.py:341` (いずれも `add_altitude_columns` の直後、窓集計の前に毎時系列を作る)、
スコア側は `mvp.py:339` / `detail.py:429` で `SUMMIT_CLOUD_CAL_VAR` を優先して読む。ほか `scratch_validate_refs.py:65` (`today=` 指定)、`scratch_r1_calibration_effect.py:37`。

**本体の適用範囲について、コードとバナーから確認できたこと**

- **夜間にも同じ表を適用している** (系列の全時刻に適用。バナー: "night hours get the same table because nothing better exists (R7)")。
- **季節の区別は無い** (月を見る処理が無い。バナー: "WARM-SEASON SAMPLE ONLY")。10 月以降は標本外の季節に適用されることになる。
- 表は T-1h と T の平均で作り、本体は単一時刻の値に適用 (バナーに明記)。
- core の import は標準ライブラリ + `requests` のみ。モジュール読み込み時にファイルを書く処理は無い (`os.makedirs` は関数内のみ)。

### 1.2 バックテスト側の較正表生成の流れ

```
data/parquet/single_runs_long.parquet   (parse_single_runs.py; 00Z ラン 2026-06-12〜09-17, git 管理下)
data/parquet/observation_long.parquet   (obs_import.py)
        │  trackb_eval.lead_day_pairs(sr, obs, "cloud_cover_at_summit")
        │    - 予報: value_h1 = T-1h と T の平均、lead_day = lead_hours // 24 + 1 (00Z 起点)
        │    - 実況: sun1h >= 0.4 h を「晴れ」(SUN_SUNNY_H)、JST 07〜17 時に終わる 1 時間 (DAY_HOURS_JST)
        │    - obs_wide(prefer="etrn_cache"): 同時刻に etrn と amedas_map があれば etrn 優先
        ▼
backtest/cloud_calibration_table.py  build()
        - fuji ペアを除外 (麓 4 地点プール)
        - ビン BIN_EDGES (本体と同じ)、lead 束ね LEAD_GROUPS: ecmwf d1-2/d3-5/d6-10、msm d1-2/d3-4
        - p_sunny (経験率) → n 重み付き PAV (quantile_map.pav_decreasing) で単調非増加 → p_sunny_used
        ▼
docs/cloud-calibration-table.json   (built 2026-09-19。source は自由文字列。table[model][i].bins[j].{n, p_sunny, p_sunny_used, thin})
docs/cloud-calibration-table.txt    (n 入りの表 + 案 A/B/C の実効雲量表)
        ▼  (手作業でコピー)
本体 core.CLOUD_CALIBRATION_TABLE    (p_sunny_used の値を直書き)
```

確認したこと:

- **既存 JSON の `p_sunny_used` と本体の `CLOUD_CALIBRATION_TABLE` は 5 行 × 8 ビンすべて一致** (0.12 と 0.120 のような表記差のみ)。ビン境界も一致。
- **現在の parquet で `build()` をメモリ上で再実行すると、既存 JSON と n・p_sunny_used が完全一致** (ファイルは書いていない)。
- JSON を生成したコミットは `e12a930` (2026-09-19 10:52)。`ee9355f` は生成スクリプトと .txt のみ変更し、JSON は変えていない。
- 案C の正規化 (`p / p_ref`) は JSON には無く、本体のコード側 (`CLOUD_CALIBRATION_P_REF`, `calibrated_cloud_pct`) にある。
  バックテスト側では `effective_tables()` が .txt に表示用として計算するだけ。

**学習標本の内訳 (現在の parquet で `lead_day_pairs` を集計、fuji 除外)**

| ペア | 山頂標高 | 実況の出所 | 件数 | valid_time 範囲 (UTC) |
|---|---|---|---|---|
| karamatsu / 白馬 48141 | 2696m | etrn_cache (〜09-14) + amedas_map (09-10〜) | 14,253 | 06-12〜09-18 |
| akadake / 野辺山 48571 | 2899m | 同上 | 14,253 | 06-12〜09-18 |
| nikko_shirane / 奥日光 41166 | 2578m | 同上 | 14,225 | 06-12〜09-18 |
| adatara / 鷲倉 36196 | 1700m | **amedas_map のみ** | **1,184** | **09-10〜09-18** |

合計 43,915 = 表の n の合計 (ecmwf 31,648 + msm 12,267) と一致。
→ 「検証済み標高 1700〜2899m」の下端 1700m は **約 8 日分・全体の 2.7%** に支えられている。メタデータには範囲と一緒に地点別 n を残す設計にする (§2.3)。

### 1.3 lead の定義の違い (事実)

- バックテスト: `lead_day = lead_hours // 24 + 1`、lead_hours は 00Z ラン起点 → d1 = 09 JST 当日〜09 JST 翌日。
- 本体: `lead_day_for` = 取得日 (系列の先頭日) からの暦日差 + 1 → d1 = 当日 00〜24 時 (本体は Forecast API で「どのランか」を持たない)。
- よって翌朝 07〜08 時 JST は、バックテストでは d1、本体では d2 になる (00Z ランを当日に取得した場合)。
- 段階2の決定: モジュールは lead を **ラン初期時刻からの経過時間 (時間)** で受け取り、表も `lead_hours: [lo, hi)` で持つ。
  本体が学習と違う行を引く割合は `backtest/r11_lead_mismatch.py` → `docs/r11-lead-mismatch.txt` で数え、product-recommendations R11 に記録した。

### 1.4 hoshibiyori について (読み取りのみで確認)

- `C:\hoshibiyori` の雲の主指標は **モデルの全層雲量 `cloud_cover`** と、気圧面雲量から作る「視界遮蔽雲量」(`step5_cloud_forecast/elevation_cloud.py`)。
  R1 表の説明変数 `cloud_cover_at_summit` とは **別の変数** なので、R1 表はそのままでは hoshibiyori に適用できない (説明変数の不一致は常にエラーにする設計、§2.4)。
- **hoshibiyori の `CLAUDE.md` は「yamabiyori 側のコードやライブラリ構成を流用・コピーしない」を原則とし、例外はその都度ユーザー承認** としている。
  `mos/` はバックテスト側で書く新規コードだが、同じものを本体にも入れる以上この原則に触れる可能性がある → **§4 の質問 Q3**。

### 1.5 段階2で確認したこと / 未確認事項

- **origin/main**: `git --no-optional-locks ls-remote origin main` = `52d3d2a5560ec35329de1b47f6a836b658db15d6` → ローカルの 52d3d2a と**同一** (差なし、2026-09-23)。
- **Python の版**: hoshibiyori の README に「Python 3.13以降を想定(開発環境は3.15 RC)」、`__pycache__` は cpython-315。
  本体の `__pycache__` は cpython-313 / 315。`mos/` は 3.8 の構文に収めた (テストで検査) ので両方で動く。3.15.0rc1 で `mos` 単体の動作を確認済み。
- **R1 の学習範囲の定義 (出典)**:
  - 日中 = **JST 07〜17 時に終わる 1 時間** (時計基準。日の出・日の入り基準ではない)。出典 `backtest/trackb_eval.py:30` `DAY_HOURS_JST = list(range(7, 18))`、
    `lead_day_pairs` がこの時刻で絞っている。標本の時刻が 7〜17 だけであることは `mos_export` が検査する。
  - 暖候期 = **valid_time (JST) が 6〜9 月**。学習コード (`cloud_calibration_table.py` / `trackb_eval.lead_day_pairs`) に季節の絞り込みは**無く**、
    標本期間 (ラン 2026-06-12〜09-17 00Z、valid 2026-06-12〜09-18 JST) で決まっている。表の `season.months` には標本に現れた月をそのまま入れた。
    別の解析 `tracka_eval.py:20` に `WARM_MONTHS = [4, 5, 6, 7, 8, 9]` があるが、R1 の学習には使われていないので採らない。
    月単位なので 6/1〜6/11 と 9/19〜9/30 は「範囲内」扱いになる (標本の実日付は `season.sampled_valid_dates_jst` に記録)。
- 未確認のまま: 本体プラグインがスクリプトをどの作業ディレクトリ / `sys.path` で起動するか。夜間・寒候期に R1 表がどの程度外れるか (データが無い)。

---

## 2. 設計 (段階2で実装したもの)

### 2.0 レビューでの決定 (2026-09-23)

| 項目 | 決定 |
|---|---|
| Q1 範囲外の挙動 | `on_mismatch` = `"error"` (既定) / `"passthrough"` / `"allow"`。時刻・月は `hour_jst`・`month` の数値で受け取る。範囲の境界は JSON に数値で持つ |
| Q2 本体の読み方 | 固定コミット `52d3d2a` を `git show` で一時フォルダへ。`git worktree add` は使わない。コミットが無ければ skip |
| Q3 hoshibiyori | コピーは今回承認しない。hoshibiyori には何も書かない |
| 追加1 lead | 表・関数とも **ラン初期時刻からの経過時間 (時間)**。本体との行違いを数える → R11 |
| 追加3 標高 | 地点別 n・日数を持たせ、日数が少ない地点は provisional |
| 追加4 本体の読み取り | git は `--no-optional-locks` の読み取り系 (`show` / `cat-file` / `rev-parse` / `ls-remote`) のみ。`fetch` しない |
| 追加5 Python | 新構文を避ける (3.8 の構文) |
| 既定案 | 表は `mos_tables/`、パッケージ名 `mos` |

### 2.1 パッケージ構成

```
mos/                              ← 移植時にディレクトリごとコピーする単位。表は含まない。標準ライブラリのみ
  __init__.py                     公開名の再エクスポート、__version__
  errors.py                       MosError / SchemaError / TableNotFoundError / ContextMismatchError
  tables.py                       MOS_VERSION, parse_table_set(), content_sha256(), Table / TableSet / Transform
  calibrate.py                    select_table(), bin_index(), probability(), effective_value(), calibrate(), Selection / Result
mos_tables/r1-summit-cloud-sunny.json   新形式の表 (生成物)
backtest/mos_export.py            表の生成 (polars 可、バックテスト側専用)
backtest/r11_lead_mismatch.py     R11 の集計 → docs/r11-lead-mismatch.txt
tests/test_mos.py                 単体テスト
tests/test_mos_equivalence.py     本体 v1.5.0 との等価性テスト
```

山頂内挿・モデルの選び方 (MSM 降水が非 null か)・lead の数え方・標高ランプ・スコアは `mos/` に入れない (本体側の糊)。

### 2.2 公開関数 (実装済み)

```python
MOS_VERSION = "0.1.0"; SUPPORTED_SCHEMA_VERSIONS = (1,); ON_MISMATCH_CHOICES = ("error", "passthrough", "allow")

parse_table_set(doc: dict) -> TableSet                  # json.load 済みの dict を検証。合わなければ SchemaError
content_sha256(doc: dict) -> str                        # content_sha256 キーを除いた正規化 JSON の SHA-256

select_table(ts, *, predictor, target, model, lead_hours, hour_jst, month, on_mismatch="error") -> Selection
    # Selection(table | None, in_scope: bool, reason: str)
probability(ts, table, value) -> float | None           # 予報値 → 表の p (PAV 済み)
effective_value(ts, table, value) -> float | None       # transform に従う出力 (R1 は案C の実効雲量 %)
calibrate(ts, value, *, predictor, target, model, lead_hours, hour_jst, month, on_mismatch="error") -> Result
    # Result(value, applied, in_scope, table_id, reason)。passthrough で範囲外なら value = 入力値, applied=False
bin_index(edges, value) -> int
```

- 選び方の順序: (predictor, target) が無い → `TableNotFoundError` (on_mismatch と無関係) → model が無い → `fallback.unknown_model`
  (R1 は ecmwf_ifs025。reason に `model_fallback:` を残す) → `lead_hours` が `[lo, hi)` に入る表 → 入らなければ `fallback.lead_out_of_range`
  (`nearest_group` = 区間までの距離が最小の表、同距離は表の先の行 / `error`) → 最後に時刻・月の範囲を照合して on_mismatch。
- `hour_jst` は valid_time の JST の「時」(0〜23。学習では「その時刻に終わる 1 時間」)、`month` は valid_time の JST の月。整数以外は `ValueError`。
- NaN は本体と同じく最終ビン扱い (本体 `calibrated_cloud_pct` と一致を確認)。

### 2.3 JSON スキーマ (schema_version 1、実装済み)

トップレベル: `schema` (`"yamabiyori-mos-table"`)、`schema_version`、`min_mos_version`、`table_set_id`、`table_set_version`、`content_sha256`、
`description`、`provenance`、`binning` (`kind: "zero_atom_right_closed"`、`edges`、`labels`)、
`transform` (`kind: "ratio_to_reference"` | `"probability"`、`scale`、`clamp`、`round_decimals`、`reference: {lead_hours, bin, same_model}`)、
`fallback` (`unknown_model`、`lead_out_of_range`)、`tables`。

各テーブル (要件 B の属性はすべてテーブル単位。provenance もテーブルごとに複製):

| キー | R1 の値 |
|---|---|
| `table_id` | `ecmwf_ifs025/h0-48` など (`h` = lead 時間) |
| `predictor` / `predictor_unit` / `predictor_definition` | `cloud_cover_at_summit` / `%` / 気圧面雲量を山頂標高へ内挿 |
| `target` / `target_definition` | `p_sunny` / `P(sun1h >= 0.4 h in the hour ending at valid_time)` |
| `model`, `lead_hours`, `lead_definition`, `lead_days_legacy` | 例 `ecmwf_ifs025`, `[0, 48]`, 経過時間 `[lo, hi)`, `[1, 2]` |
| `time_of_day` | `{label: "day", basis: "clock_jst_hour_ending", hours_jst: [7..17], source: "backtest/trackb_eval.py DAY_HOURS_JST"}` |
| `season` | `{label: "warm", basis: "valid_month_jst", months: [6,7,8,9], sampled_valid_dates_jst: ["2026-06-12","2026-09-18"], source: ...}` |
| `observation` | `kind: "foot_amedas_sun1h"`、組の作り方 (麓の日照 vs 山頂予報 = 代表性誤差込み)、etrn 優先、観測所一覧 |
| `training_period` | `run_init_utc`、`run_hours_utc: [0]`、`forecast_value: "mean of T-1h and T"` |
| `validated_elevation` | 下記 |
| `provenance` | `generator` (`backtest/cloud_calibration_table.py (build)`)、`exporter`、`commit`、`dirty`、`exported`、`source_sha256` (関係 5 ファイル)、`legacy_json` (`path`、`commit` = e12a930、`built`、`identical`) |
| `n` / `p_all` / `bins[]` | `bins[]` = `{label, n, p_raw (PAV 前), p (計算に使う値), thin}` |

**検証済み標高 (`validated_elevation`)** — ecmwf_ifs025/h0-48 の実値 (表ファイルから転記):

```json
{"provisional_rule": {"min_days": 30, "days": "distinct JST dates of valid_time in this table's sample"},
 "confirmed_m": [2578, 2899], "including_provisional_m": [1700, 2899],
 "sites": [{"site_id": "adatara",       "elevation_m": 1700, "station_name": "鷲倉",   "n": 160,  "days": 8,  "share": 0.025, "status": "provisional"},
           {"site_id": "akadake",       "elevation_m": 2899, "station_name": "野辺山", "n": 2056, "days": 99, "share": 0.325, "status": "confirmed"},
           {"site_id": "karamatsu",     "elevation_m": 2696, "station_name": "白馬",   "n": 2056, "days": 99, "share": 0.325, "status": "confirmed"},
           {"site_id": "nikko_shirane", "elevation_m": 2578, "station_name": "奥日光", "n": 2052, "days": 99, "share": 0.324, "status": "confirmed"}]}
```

(表ファイルでは各地点に `name`・`station_id` も入る。5 表とも安達太良山は 8 日で provisional、他 3 地点は 94〜99 日で confirmed)

- **閾値: 地点の標本日数 (valid_time の JST 日付の数) が 30 日未満なら provisional**。
  理由: 同じ日の時間どうしは天気の型を共有して独立でないので、時間数 (n) ではなく日数で数える。30 は既存のビン単位の下限 `MIN_N = 30`
  (`cloud_calibration_table.py`) にそろえた判断値で、統計的に導いたものではない。R1 では安達太良山 8 日に対し他 3 地点は 94〜99 日で、
  閾値を 9〜94 日のどこに置いても分かれ方は変わらない。
- `mos` の関数は標高を見ない (山固有の処理を入れない方針)。呼び出し側が `validated_elevation` を読んで表示・判断に使う。

### 2.4 範囲外の挙動 (決定: Q1)

| on_mismatch | 挙動 | 使う場面 |
|---|---|---|
| `"error"` (既定) | `ContextMismatchError` | hoshibiyori など、範囲外を引いたら気づきたい呼び出し |
| `"passthrough"` | 入力値をそのまま返す、`applied=False` | 範囲外は補正しないと決めた呼び出し |
| `"allow"` | 表を使う、`in_scope=False` | v1.5.0 と同じ出力を保つ本体 (範囲外であることは `in_scope` で分かる) |

説明変数・目的変数の不一致は常に `TableNotFoundError`。本体に入れるときの `allow` / `passthrough` の選択は本体側の判断 (R11)。

### 2.5 版と互換性の検査 (実装済み)

`parse_table_set` が検査するもの: `schema` 名、`schema_version ∈ SUPPORTED_SCHEMA_VERSIONS`、`min_mos_version <= MOS_VERSION`、
`content_sha256` (手編集・取り違えの検出)、必須キー、ビン数 = `len(edges)`、p ∈ [0, 1]、`time_of_day` / `season` のラベルと数値、
lead 区間の重なり、`table_id` の重複、fallback 先の存在、各モデルの基準表 (案C の p_ref) の存在と p_ref > 0。

### 2.6 新形式の表の生成経路 (実装済み)

`python -m backtest.mos_export` の 1 経路にした (段階1 案の `from-legacy` / `rebuild` の 2 本立てはやめた)。
理由: 地点別 n・日数や月は既存 JSON に無く parquet から数えるしかないので、数値も同じ `cloud_calibration_table.build()` で parquet から出し、
**既存 JSON と n・p_raw・p が一致しなければ止まる** ようにした方が経路が 1 本で済む (一致は確認済み: `legacy_json.identical = true`)。
既存の `cloud_calibration_table.py` と `docs/cloud-calibration-table.{json,txt}` は変更していない。

`provenance.commit` は書き出し時の HEAD、`dirty` は作業ツリーが clean でないとき true。現在の表ファイルは段階2の新規ファイルが未コミットの状態で
書いたので `dirty: true` (commit 8032cb1)。コミット後に `python -m backtest.mos_export` を再実行すると clean な provenance になる (数値は変わらない)。

### 2.7 移植手順

1. バックテスト側で `pytest tests/test_mos.py tests/test_mos_equivalence.py` が緑であること。
2. `mos/` をディレクトリごとコピー (選り抜かない)。表は `mos_tables/<id>.json` を別途コピー。
3. 起動時に `mos.parse_table_set(json.load(...))` を 1 回。
4. 移植先に版の固定テストを 1 本: `mos.MOS_VERSION`、表の `table_set_version`・`content_sha256` が期待値と一致すること。コピー元のコミットを移植先の CHANGELOG に記録。
5. 本体: `calibrated_summit_cloud_series` の中を「本体の糊 (モデル選択・lead・標高ランプの混合) + `mos.calibrate(..., on_mismatch=...)`」に置き換える。
   v1.5.0 と同じ出力にするなら `tests/test_mos_equivalence.py` の `body_glue` がそのまま雛形 (lead_day d を 24·(d−1) 時間で渡し、`allow`)。
   lead を正しく数えるなら、ラン初期時刻を得て経過時間を渡す (R11)。
6. hoshibiyori: 今回は承認されていない。夜間・全層雲量 (`cloud_cover`) の表ができた時点で改めて判断 (hoshibiyori の CLAUDE.md のコピー禁止原則にも関わる)。

### 2.8 等価性テスト (実装済み、`tests/test_mos_equivalence.py`)

- 本体: `git --no-optional-locks -C C:/mountain-weather` で `cat-file -e 52d3d2a^{commit}` (無ければ skip) → `rev-parse 52d3d2a:mountain_weather_core.py`
  が `1aaf5d8…` であること → `show` の中身を pytest の `tmp_path` に書いて `importlib` で読み込む (`sys.dont_write_bytecode = True`)。
- 比較:
  1. 表: ビン境界・5 行 × 8 ビンの p・lead 区間 (d → 24·(d−1)〜24·d 時間)・`CLOUD_CALIBRATION_P_REF` が完全一致。
  2. 1 値: `core.calibrated_cloud_pct(v, model, lead_day)` と `mos.calibrate(...).value` が `==` かつ型も一致。
     v = 境界値・負値・100 超・None・NaN + 0〜100 の 0.5 刻み (計 221 値) × model 3 (未知モデル含む) × lead_day 1〜16 × 範囲内/範囲外 (allow)。
  3. lead_day d の中のどの経過時間 [24(d−1), 24d) を渡しても本体と同じ値。
  4. 系列: `core.calibrated_summit_cloud_series` と `body_glue` (本体の糊 + mos) を、既存フィクスチャ (Single Runs 5 地点、MSM 有無の組み合わせ 3 通り) と、
     月をまたぎ None・NaN・MSM 有無が混ざる合成系列 (today 3 通り × 山頂 3 標高) で比較。
- 実装中に分かったこと: 本体の系列関数は標高ランプの混合 `round(w*cal + (1-w)*raw, 1)` を w=1 でも通すので、raw が NaN だと `0.0*NaN` で NaN になる
  (mos 単体と `calibrated_cloud_pct` は NaN を最終ビンとして 100 を返す)。混合は本体の糊なので `body_glue` 側で同じ式を使って一致させた。
  Open-Meteo の欠損は null (None) で来るので、実運用で NaN が入る経路があるかは未確認。
  → 2026-09-23 確認 (本体 52d3d2a を `git show` で読んだ): null は `resp.json()` / キャッシュの `json.load` で None になり、MSM/ECMWF の結合
  (mvp/detail `fetch_forecast`: `if v is None` で補完)、`level_profile` (None の面を除外)、`interp_at_altitude` (全面欠損なら None) を通って
  None のまま `calibrated_cloud_pct` に届き、None を返す。null から NaN が生じる経路は無い。本体で NaN を作るのは地形タイル
  (`mountain_terrain.py` の `np.nan`) だけで、雲量の経路とは交わらない。NaN が入りうるのは API 応答自体に `NaN` トークンが入る場合だけで、
  本体キャッシュ 1,170 件・本リポジトリの生データ/スナップショット 342 件に `NaN`/`Infinity` は 0 件。→ R12 としては記録しない。
- 結果: 等価性 9 件・単体 51 件を含む全 127 件が緑 (venv, Python 3.13)。

---

## 3. 既存の解析結果・収集系に影響が無いことの確認方法

- 作業前に 4 リポジトリ (`C:/mountain-weather`, `C:/mountain-weather-r8`, `C:/hoshibiyori`, 本リポジトリ) の `rev-parse HEAD` と `status --porcelain` を保存し、
  作業後に `--no-optional-locks` で取り直して比較する。本リポジトリ以外は完全一致、本リポジトリは追加ファイルと 2 ファイルへの追記 (README, product-recommendations) のみ。
- `git diff --stat HEAD -- docs/ backtest/collect_* backtest/plugin_confidence_snapshot.py .github/` で、変更が `docs/product-recommendations.md` の追記だけであること。
  追記は R10 と「推奨の一覧」の間への R11 節の挿入と、一覧の末尾への 1 行追加で、削除行 0。
- 本体ディレクトリ (`.git` を含む) に、段階2の開始以降の更新時刻を持つファイルが無いこと (`find -newer`)。
  - 結果 (2026-09-23): 作業ツリーのファイルと `.git/index` は変化なし。ただし **全テスト (`pytest -q`) を回すと `C:/mountain-weather/.git` ディレクトリの更新時刻が変わる**。
    原因は既存の `tests/test_ensemble.py` → `backtest/plugin_confidence_snapshot.py:55` の `git status --porcelain` (`--no-optional-locks` 無し) が
    index のロックファイルを作って消すため (index 自体は書き換わらない)。新規の `tests/test_mos*.py` だけを回したときは `.git` の更新時刻が変わらないことを確認した。
    `plugin_confidence_snapshot.py` は収集系なので今回は触っていない (直すなら `git --no-optional-locks` を足すだけ。系列への影響は無いはずだが判断は保留)。
  - その後の対応 (2026-09-23): 指示を受けて `2a56ee5` で `--no-optional-locks` を追加。修正前後で envelope が同じことを `tests/test_plugin_identity.py` で確認し、
    修正後は全テストを回しても本体 `.git` の更新時刻が変わらないことを確認した。
  - `plugin_identity` の既知の癖: `stdout.strip()` で `git_dirty_files` の 1 行目の先頭の空白 (porcelain の状態列) が落ちる。envelope を変えないため修正しない。

---

## 4. 残っている判断事項 (本体側、今回はスコープ外)

- 本体に入れるときの `on_mismatch` (`allow` で v1.5.0 を保つか、`passthrough` で範囲外を生値にするか)。10 月以降は全時刻が範囲外 (R11)。
- 本体の lead をラン初期時刻基準に直すか (R11)。直すと v1.5.0 と出力が変わる。
