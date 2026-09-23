"""R12 の修正案の検証: R1 の MSM 行を「本体と同じ 7 面 (900/800hPa あり) の山頂雲量」で学習し直す。本体は変更しない。

データ: Historical Forecast API の jma_msm (7 面)、麓 4 地点 (R1 の学習と同じ: karamatsu/白馬, akadake/野辺山, nikko_shirane/奥日光, adatara/鷲倉)。
評価・学習に使う (地点, valid_time) の組は、既存の学習コード (trackb_eval.lead_day_pairs の jma_msm、fuji 除外、日中 07-17 時) と同じ組に限る
(lead が違っても valid_time が同じなら 1 組。Historical にはリードの区別が無い)。実況・晴れの定義・value_h1 (T−1h/T 平均)・ビン・PAV も学習コードと同じ。

比べる表 (いずれも MSM の lead 0-48h の行):
  T_old = 現行 R1 (Single Runs・5 面, 4 地点全部で学習; この評価の実況にとって標本内)
  T_H5  = Historical・5 面 (900/800 を除いて内挿) で学習
  T_H7  = Historical・7 面 (本体と同じ内挿) で学習 = 新表の案
T_H5 / T_H7 の評価は地点を 1 つずつ外す (leave-one-site-out)。組み合わせ:
  A = T_old を 7 面の入力に当てる (今の本体)      B = T_H7 を 7 面に (提案)
  C = T_old を 5 面の入力に当てる (学習と同じ)    D = T_H5 を 5 面に
  B − A = 提案の効果、D − C = データ源の違い (面は同じ 5 面)、B − D = 面の数の違い (データ源は同じ Historical)。

    python -m backtest.r12_msm_retrain fetch      # 生 JSON を data/raw/historical_forecast_r12/ に保存 (保存済みはスキップ、sleep あり)
    python -m backtest.r12_msm_retrain usage      # 本体で MSM の d3-4 行が引かれる時間数 (Forecast スナップショットから)
    python -m backtest.r12_msm_retrain eval       # → docs/r12-msm-retrain-tables.txt
    python -m backtest.r12_msm_retrain export     # → mos_tables/r1-summit-cloud-sunny-msm7.json (既存の表は変更しない)
    python -m backtest.r12_msm_retrain transplant          # 移植表の評価 → docs/r12-msm-transplant-tables.txt
    python -m backtest.r12_msm_retrain export-transplant   # → mos_tables/r1-summit-cloud-sunny-msm7-transplant.json

移植表 (T): 旧表の MSM d1-2 行 (Single Runs の本物の lead 0-48h の絶対水準) に、「Historical 7 面表 ÷ Historical 5 面表」のビンごとの比を掛けた行。
面の数の効果だけを移し、データ源 (実質リードの短さ) の水準は持ち込まない狙い。基準値 (0% ビン) も同じ比で移す。
"""
import argparse
import copy
import gzip
import json
import logging
import random
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import polars as pl
import requests

import mos

from . import config
from . import mos_export as mx
from . import skill
from . import summit_interp as si
from . import winter_r1 as w
from .cloud_calibration_table import BIN_EDGES, MIN_N, bin_of
from .quantile_map import pav_decreasing
from .trackb_eval import OBS_PATH, SR_PATH, fmt, lead_day_pairs

log = logging.getLogger("backtest")

REPO = config.DATA_DIR.parent
RAW_DIR = config.DATA_DIR / "raw" / "historical_forecast_r12"
SNAP_DIR = config.DATA_DIR / "snapshots" / "forecast"
OUT_TXT = REPO / "docs" / "r12-msm-retrain-tables.txt"
OUT_TABLE = REPO / "mos_tables" / "r1-summit-cloud-sunny-msm7.json"
OUT_TP_TXT = REPO / "docs" / "r12-msm-transplant-tables.txt"
OUT_TP_TABLE = REPO / "mos_tables" / "r1-summit-cloud-sunny-msm7-transplant.json"
OLD_TABLE = REPO / "mos_tables" / "r1-summit-cloud-sunny.json"
MODEL = "jma_msm"
SITE_IDS = ["karamatsu", "akadake", "nikko_shirane", "adatara"]
SITES = [s for s in config.SITES if s["site_id"] in SITE_IDS]
PERIOD = (date(2026, 6, 11), date(2026, 9, 18))
LEVELS_7 = list(si.LEVEL_STACK_HPA)
LEVELS_5 = list(w.LEVELS_NO_900_800)
N_BOOT = 2000
SEED = 20260923
JST = timedelta(hours=9)


# ---------------------------------------------------------------- 取得
def fetch_all() -> int:
    session = requests.Session()
    session.headers["User-Agent"] = "yamabiyori-backtest (R12 MSM retrain check)"
    n = 0
    for a, b in w.month_chunks(*PERIOD):
        if w.fetch_chunk(session, MODEL, a, b, sites=SITES, raw_dir=RAW_DIR):
            n += 1
            time.sleep(w.SLEEP_SECONDS)
    return n


# ---------------------------------------------------------------- d3-4 行の使用頻度
def usage() -> str:
    """Forecast API の jma_msm スナップショット (本体と同じ API) で、本体が MSM の d3-4 行を引く時間 (= MSM の降水が非 null で、
    取得日 (JST) を 1 とする暦日差が 3〜4 の時間) を数える。本体 core.calibrated_summit_cloud_series のモデル選択と lead_day_for と同じ規則。"""
    rows = []
    for path in sorted(SNAP_DIR.glob("*/*_jma_msm.json.gz")):
        with gzip.open(path, "rt", encoding="utf-8") as f:
            env = json.load(f)
        fetched = datetime.fromisoformat(env["fetched_at"])
        today = (fetched + JST).date()
        loc = env["body"][0]                               # 5 地点ともホライズンは同じなので 1 地点で数える
        times, precip = loc["hourly"]["time"], loc["hourly"]["precipitation"]
        counts = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
        day34 = 0
        last = None
        for t, v in zip(times, precip):
            if v is None:
                continue
            local = datetime.fromisoformat(t) + JST        # スナップショットは timezone=UTC
            if local.date() < today:
                continue
            ld = (local.date() - today).days + 1
            counts[min(ld, 5)] += 1
            if ld in (3, 4):
                day34 += 7 <= local.hour <= 17
            last = local
        rows.append({"snapshot": f"{path.parent.name}/{path.name[:3]}", "fetched_jst": (fetched + JST).strftime("%m-%d %H:%M"),
                     "d1": counts[1], "d2": counts[2], "d3": counts[3], "d4": counts[4], "d5+": counts[5],
                     "d3_4_hours": counts[3] + counts[4], "d3_4_daytime_07_17": day34,
                     "last_msm_hour_jst": last.strftime("%m-%d %H時") if last else None})
    d = pl.DataFrame(rows)
    summary = d.select(pl.len().alias("n_snapshots"), pl.col("d3_4_hours").mean().alias("mean_d3_4_hours"),
                       pl.col("d3_4_hours").min().alias("min"), pl.col("d3_4_hours").max().alias("max"),
                       pl.col("d3_4_daytime_07_17").mean().alias("mean_d3_4_daytime"))
    return ("## 本体で MSM の d3-4 行が引かれる時間 (Forecast API スナップショットで数えた)\n"
            "規則: MSM の降水が非 null の時間は jma_msm の行 (core.calibrated_summit_cloud_series)、lead_day = 取得日 (JST) からの暦日差 + 1 (core.lead_day_for)。\n"
            "d1〜d5+ = MSM の値がある時間数 (取得日 00 時 JST 以降)。d3_4_daytime_07_17 = そのうち日中 (07-17 時)。\n"
            + fmt(d, 0) + "\n" + fmt(summary, 1) + "\n")


# ---------------------------------------------------------------- 評価
def frame() -> pl.DataFrame:
    """学習と同じ (地点, valid_time) の組に、Historical の 7 面 / 5 面の山頂雲量 (value_h1) を付ける。"""
    sr, obs = pl.read_parquet(SR_PATH), pl.read_parquet(OBS_PATH)
    p = (lead_day_pairs(sr, obs, "cloud_cover_at_summit").filter((pl.col("model") == MODEL) & ~pl.col("pair").str.starts_with("fuji"))
         .with_columns(pl.col("pair").str.split("/").list.first().alias("site_id"))
         .select("site_id", "valid_time", "obs_sunny").unique(["site_id", "valid_time"]))
    rows = []
    sites = {s["site_id"]: s for s in SITES}
    for site_id, h in w.load_site_hourly(MODEL, raw_dir=RAW_DIR).items():
        elev = sites[site_id]["elevation_m"]
        c7 = si.series_at_altitude(h, "cloudcover", elev, LEVELS_7)
        c5 = si.series_at_altitude(h, "cloudcover", elev, LEVELS_5)
        times = [datetime.fromisoformat(t).replace(tzinfo=timezone.utc) for t in h["time"]]
        for i in range(1, len(times)):
            if times[i] - times[i - 1] != timedelta(hours=1) or None in (c7[i], c7[i - 1], c5[i], c5[i - 1]):
                continue
            rows.append((site_id, times[i], (c7[i] + c7[i - 1]) / 2, (c5[i] + c5[i - 1]) / 2))
    hist = pl.DataFrame(rows, schema={"site_id": pl.String, "valid_time": pl.Datetime("us", "UTC"), "c7": pl.Float64, "c5": pl.Float64},
                        orient="row")
    d = p.join(hist, on=["site_id", "valid_time"], how="inner")
    jst = pl.col("valid_time").dt.convert_time_zone("Asia/Tokyo")
    return d.with_columns(jst.dt.date().alias("date_jst"), jst.dt.month().alias("month_jst"), jst.dt.hour().alias("hour_jst"),
                          pl.col("obs_sunny").cast(pl.Int8).alias("y"),
                          pl.col("c7").map_elements(bin_of, return_dtype=pl.Int64).alias("b7"),
                          pl.col("c5").map_elements(bin_of, return_dtype=pl.Int64).alias("b5")).sort("site_id", "valid_time")


def fit_row(d: pl.DataFrame, bcol: str) -> dict:
    """cloud_calibration_table.build と同じ: ビンごとの晴れ率 → n 重み付き PAV (空のビンは 0.0・重み 1)。"""
    nb = len(BIN_EDGES)
    n = [0] * nb
    k = [0] * nb
    for b, y in zip(d[bcol].to_list(), d["y"].to_list()):
        n[b] += 1
        k[b] += y
    raw = [(k[i] / n[i]) if n[i] else None for i in range(nb)]
    used = pav_decreasing([r if r is not None else 0.0 for r in raw], [max(x, 1) for x in n])
    return {"n": n, "p_raw": [round(r, 3) if r is not None else None for r in raw], "p": [round(u, 3) for u in used]}


def predictions(d: pl.DataFrame, old_p: list) -> pl.DataFrame:
    parts = []
    for s in SITE_IDS:
        test = d.filter(pl.col("site_id") == s)
        if test.is_empty():
            continue
        train = d.filter(pl.col("site_id") != s)
        h5, h7 = fit_row(train, "b5")["p"], fit_row(train, "b7")["p"]
        parts.append(test.with_columns(
            pl.col("b7").map_elements(lambda b: old_p[b], return_dtype=pl.Float64).alias("A_old_on7"),
            pl.col("b7").map_elements(lambda b, t=h7: t[b], return_dtype=pl.Float64).alias("B_h7_on7"),
            pl.col("b5").map_elements(lambda b: old_p[b], return_dtype=pl.Float64).alias("C_old_on5"),
            pl.col("b5").map_elements(lambda b, t=h5: t[b], return_dtype=pl.Float64).alias("D_h5_on5"),
            (1 - pl.col("c7").clip(0, 100) / 100).alias("raw7"), (1 - pl.col("c5").clip(0, 100) / 100).alias("raw5")))
    return pl.concat(parts)


VARIANTS = ["A_old_on7", "B_h7_on7", "C_old_on5", "D_h5_on5", "raw7", "raw5"]
CONTRASTS = [("提案の効果 (B − A, 7 面の入力)", "B_h7_on7", "A_old_on7"),
             ("データ源の違い (D − C, 5 面の入力)", "D_h5_on5", "C_old_on5"),
             ("面の数の違い (B − D, Historical 同士)", "B_h7_on7", "D_h5_on5")]


def brier(p: list, y: list) -> float:
    return sum((a - b) ** 2 for a, b in zip(p, y)) / len(y)


def auc_of(p: list, y: list) -> float:
    return skill.auc(p, [bool(v) for v in y])[0]


def boot(d: pl.DataFrame, a: str, b: str, n_boot: int = N_BOOT, seed: int = SEED) -> dict:
    """日単位ブロックのブートストラップ (日の並びを固定、seed 固定) で ΔBrier と ΔAUC の 95% 区間。"""
    days = sorted(set(d["date_jst"].to_list()))
    by_day = {k: g for (k,), g in d.group_by("date_jst")}
    cols = {dd: (by_day[dd][a].to_list(), by_day[dd][b].to_list(), by_day[dd]["y"].to_list()) for dd in days}
    rng = random.Random(seed)
    db, da = [], []
    for _ in range(n_boot):
        pa, pb, yy = [], [], []
        for _ in days:
            x = cols[days[rng.randrange(len(days))]]
            pa += x[0]; pb += x[1]; yy += x[2]
        db.append(brier(pa, yy) - brier(pb, yy))
        da.append(auc_of(pa, yy) - auc_of(pb, yy))
    db.sort(); da.sort()
    lo, hi = int(0.025 * n_boot), int(0.975 * n_boot) - 1
    return {"d_brier_ci95": (db[lo], db[hi]), "p_boot_d_brier_lt0": sum(x < 0 for x in db) / n_boot,
            "d_auc_ci95": (da[lo], da[hi])}


def score_table(d: pl.DataFrame) -> pl.DataFrame:
    y = d["y"].to_list()
    base = sum(y) / len(y)
    rows = [{"variant": v, "n": len(y), "brier": brier(d[v].to_list(), y), "brier_clim": base * (1 - base), "auc": auc_of(d[v].to_list(), y)}
            for v in VARIANTS]
    return pl.DataFrame(rows)


def contrast_table(d: pl.DataFrame, contrasts=None) -> pl.DataFrame:
    y = d["y"].to_list()
    rows = []
    for name, a, b in (contrasts or CONTRASTS):
        bt = boot(d, a, b)
        rows.append({"contrast": name, "d_brier": brier(d[a].to_list(), y) - brier(d[b].to_list(), y),
                     "d_brier_lo": bt["d_brier_ci95"][0], "d_brier_hi": bt["d_brier_ci95"][1], "p_boot_first_better": bt["p_boot_d_brier_lt0"],
                     "d_auc": auc_of(d[a].to_list(), y) - auc_of(d[b].to_list(), y), "d_auc_lo": bt["d_auc_ci95"][0], "d_auc_hi": bt["d_auc_ci95"][1]})
    return pl.DataFrame(rows)


def fold_table(d: pl.DataFrame, contrasts=None) -> pl.DataFrame:
    rows = []
    for (s,), g in d.group_by("site_id", maintain_order=True):
        y = g["y"].to_list()
        r = {"held_out_site": s, "n": g.height, "days": g["date_jst"].n_unique()}
        for name, a, b in (contrasts or CONTRASTS):
            r[name.split(" (")[0] + " ΔBrier"] = brier(g[a].to_list(), y) - brier(g[b].to_list(), y)
        rows.append(r)
    return pl.DataFrame(rows).sort("held_out_site")


def reliability(d: pl.DataFrame) -> pl.DataFrame:
    """入力のビンごと: 各案の予報 P の平均と実況の晴れ率 (A/B は 7 面のビン、C/D は 5 面のビン)。"""
    out = []
    for bcol, vs in (("b7", ["A_old_on7", "B_h7_on7"]), ("b5", ["C_old_on5", "D_h5_on5"])):
        g = (d.group_by(bcol).agg(pl.len().alias("n"), pl.col("y").mean().alias("obs_sunny"), *[pl.col(v).mean().alias(v) for v in vs])
             .rename({bcol: "bin"}).with_columns(pl.lit(bcol).alias("input")).sort("bin"))
        out.append(g.select("input", "bin", "n", "obs_sunny", *vs))
    return pl.concat(out, how="diagonal")


def effective(p: list, p_ref: float) -> list:
    return [round(min(100.0, max(0.0, 100.0 * (1.0 - x / p_ref))), 1) for x in p]


def evaluate() -> str:
    pl.Config.set_tbl_width_chars(260)
    old = json.loads(OLD_TABLE.read_text(encoding="utf-8"))
    old_msm = {t["table_id"]: t for t in old["tables"] if t["model"] == MODEL}
    old_p = [b["p"] for b in old_msm["jma_msm/h0-48"]["bins"]]
    d = frame()
    pr = predictions(d, old_p)
    full7, full5 = fit_row(d, "b7"), fit_row(d, "b5")
    p_ref_old, p_ref_new = old_p[0], full7["p"][0]
    d34 = [b["p"] for b in old_msm["jma_msm/h48-96"]["bins"]]
    rows = pl.DataFrame({"bin": [str(x) for x in mx.BIN_LABELS], "n_h7": full7["n"], "p_old": old_p, "p_h5": full5["p"], "p_h7_new": full7["p"],
                         "eff_old_d12": effective(old_p, p_ref_old), "eff_new_d12": effective(full7["p"], p_ref_new),
                         "eff_d34_oldref": effective(d34, p_ref_old), "eff_d34_if_newref": effective(d34, p_ref_new)})
    txt = ("## R12 修正案: MSM の d1-2 行を 7 面の山頂雲量で学習し直す (Historical Forecast API)\n"
           f"標本: 学習と同じ (地点, valid_time) の組のうち Historical の値があるもの。{d.height} 時間、{d['date_jst'].n_unique()} 日、地点 {sorted(set(d['site_id']))}。\n"
           "A = 旧表を 7 面の入力に (今の本体)、B = Historical 7 面表を 7 面に (提案)、C = 旧表を 5 面に (学習と同じ)、D = Historical 5 面表を 5 面に。\n"
           "B・D は地点を 1 つずつ外して学習 (leave-one-site-out)。旧表は 4 地点全部で学習済み (この実況にとって標本内) なので A・C はその分有利。\n"
           f"区間は日単位ブロックのブートストラップ ({N_BOOT} 回, seed {SEED}, 日の並びは固定)。\n\n"
           "### 各案の Brier / AUC (日中 07-17 時)\n" + fmt(score_table(pr), 4)
           + "\n\n### 差 (前 − 後、負 = 前の方が Brier が良い; ΔAUC は正 = 前の方が良い)\n" + fmt(contrast_table(pr), 4)
           + "\n\n### 地点を外したときの差 (外した地点での ΔBrier)\n" + fmt(fold_table(pr), 4)
           + "\n\n### 信頼度 (入力のビンごと: 予報 P の平均と実況の晴れ率)\n" + fmt(reliability(pr), 3)
           + "\n\n### 表の値 (4 地点全部で学習した新しい d1-2 行) と実効雲量 (案C)\n"
           + f"基準値 P_ref (0% ビン): 旧 {p_ref_old} (Single Runs・5 面) → 新 {p_ref_new} (Historical・7 面)、差 {p_ref_new - p_ref_old:+.3f}\n"
           "eff_d34_oldref = d3-4 行を旧基準値で正規化 (新表で採用 = v1.5.0 と同じ値)、eff_d34_if_newref = 新基準値で正規化した場合 (採用しない; 参考)。\n"
           + fmt(rows, 3) + "\n")
    return txt


# ---------------------------------------------------------------- 新表
def build_new_table(git: dict, today: date) -> dict:
    old = json.loads(OLD_TABLE.read_text(encoding="utf-8"))
    mos.parse_table_set(old)
    d = frame()
    full7 = fit_row(d, "b7")
    old_by_id = {t["table_id"]: t for t in old["tables"]}
    p_ref_old_msm = old_by_id["jma_msm/h0-48"]["bins"][0]["p"]
    p_ref_ecmwf = old_by_id["ecmwf_ifs025/h0-48"]["bins"][0]["p"]
    copied_from = {"table_set_id": old["table_set_id"], "table_set_version": old["table_set_version"],
                   "content_sha256": old["content_sha256"], "path": "mos_tables/r1-summit-cloud-sunny.json"}
    provenance = {"generator": "backtest/r12_msm_retrain.py (export)", "commit": git["commit"], "dirty": git["dirty"],
                  "exported": today.isoformat(), "source_sha256": mx.source_sha256() | {
                      p: __import__("hashlib").sha256((REPO / p).read_bytes()).hexdigest()
                      for p in ["backtest/r12_msm_retrain.py", "backtest/winter_r1.py"] if (REPO / p).exists()},
                  "old_table": copied_from}
    single_runs_5 = {"api": "Single Runs API (00Z runs, lead-controlled)", "levels_hpa": [1000, 925, 850, 700, 600], "n_levels": 5,
                     "note": "900/800hPa are null in Single Runs for both models"}
    tables = []
    for t in old["tables"]:
        t = copy.deepcopy(t)
        if t["table_id"] == "jma_msm/h0-48":
            continue
        t["training_source"] = single_runs_5
        if t["model"] == "ecmwf_ifs025":
            t["normalization"] = {"p_ref": p_ref_ecmwf, "source": "ecmwf_ifs025/h0-48 bin 0 of the v1.5.0 table (Single Runs, 5 levels); unchanged"}
            t["status"] = "copied unchanged from the v1.5.0 table (R12 does not affect ECMWF: 900/800hPa are null there too)"
        else:
            t["normalization"] = {"p_ref": p_ref_old_msm,
                                  "source": "jma_msm/h0-48 bin 0 of the v1.5.0 table (Single Runs, 5 levels) -- kept so this row's effective "
                                            "values stay identical to v1.5.0; NOT the new h0-48 row's bin 0"}
            t["status"] = ("5 面で学習、未更新 (trained on 5-level summit cloud; not retrained). Input mismatch with the plugin's 7-level "
                           "interpolation remains for this row; to be rebuilt from Forecast snapshots (7 levels, lead-aware) in 2027-03.")
        t["copied_from"] = copied_from
        tables.append(t)
    g = d.with_columns(pl.col("site_id"))
    months = sorted(set(d["month_jst"].to_list()))
    new = {
        "table_id": "jma_msm/h0-48",
        "predictor": "cloud_cover_at_summit", "predictor_unit": "%",
        "predictor_definition": "Open-Meteo pressure-level cloud cover interpolated to the summit elevation linearly in geopotential "
                                "height each hour, using ALL available levels 1000/925/900/850/800/700/600hPa (same as the plugin's "
                                "Forecast API fetch)",
        "target": "p_sunny", "target_definition": f"P(sun1h >= {w.SUN_SUNNY_H} h in the hour ending at valid_time)",
        "model": MODEL, "lead_hours": [0, 48],
        "lead_definition": "table slot for hours 0-48 from the run's initial time. Trained on the Historical Forecast API series (latest "
                           "forecasts stitched together; effective lead is short and not controlled), so this row likely represents "
                           "the short end of 0-48h.",
        "lead_days_legacy": [1, 2],
        "time_of_day": {"label": "day", "basis": "clock_jst_hour_ending", "hours_jst": sorted(set(d["hour_jst"].to_list())),
                        "source": "backtest/trackb_eval.py DAY_HOURS_JST (same (site, valid_time) set as the v1.5.0 training pairs)"},
        "season": {"label": "warm", "basis": "valid_month_jst", "months": months,
                   "sampled_valid_dates_jst": [d["date_jst"].min().isoformat(), d["date_jst"].max().isoformat()],
                   "source": "months present in the training sample"},
        "observation": old_by_id["jma_msm/h0-48"]["observation"],
        "training_period": {"valid_utc": [d["valid_time"].min().isoformat(), d["valid_time"].max().isoformat()],
                            "forecast_value": "mean of T-1h and T (value_h1); applied to single hourly values",
                            "pairs": "the (site, valid_time) set of the v1.5.0 jma_msm training pairs, deduplicated over leads"},
        "training_source": {"api": "Historical Forecast API (stitched latest forecasts; lead not controlled)", "levels_hpa": LEVELS_7,
                            "n_levels": 7, "raw": "data/raw/historical_forecast_r12/jma_msm/"},
        "normalization": {"p_ref": full7["p"][0], "source": "this row (jma_msm/h0-48, Historical 7 levels) bin 0 -- case C keeps "
                                                            "0% -> effective 0% for lead 0-48h"},
        "validated_elevation": mx.validated_elevation(g),
        "provenance": provenance,
        "status": "candidate (R12 fix); evaluated in docs/r12-msm-retrain.md",
        "n": d.height, "p_all": round(sum(d["y"].to_list()) / d.height, 3),
        "bins": [{"label": lab, "n": n, "p_raw": pr, "p": p, "thin": n < MIN_N}
                 for lab, n, pr, p in zip(mx.BIN_LABELS, full7["n"], full7["p_raw"], full7["p"])],
    }
    # 表の順: ecmwf 3 行、msm h0-48 (新)、msm h48-96 (旧)
    tables = [t for t in tables if t["model"] == "ecmwf_ifs025"] + [new] + [t for t in tables if t["model"] == MODEL]
    doc = {
        "schema": mos.SCHEMA_NAME, "schema_version": 2, "min_mos_version": "0.2.0",
        "table_set_id": "r1-summit-cloud-sunny-msm7", "table_set_version": "1.1.0",
        "description": "R1 with the jma_msm 0-48h row retrained on 7-level summit cloud (R12 candidate). ECMWF rows and the jma_msm "
                       "48-96h row are copied from r1-summit-cloud-sunny 1.0.0 with their original normalization, so their effective "
                       "values equal plugin v1.5.0. Needs mos >= 0.2.0 (per-row normalization).",
        "provenance": provenance,
        "binning": old["binning"],
        "transform": {"kind": "ratio_to_reference", "scale": 100, "clamp": [0, 100], "round_decimals": 1,
                      "note": "effective = 100 * (1 - p / p_ref) with each row's normalization.p_ref (schema 2)"},
        "fallback": old["fallback"],
        "tables": tables,
    }
    doc["content_sha256"] = mos.content_sha256(doc)
    return doc


# ---------------------------------------------------------------- 移植表 (旧表 + 面の数の効果)
TP_CONTRASTS = [("移植表 − 今の本体 (T − A)", "T_tp_on7", "A_old_on7"),
                ("新表 − 今の本体 (B − A)", "B_h7_on7", "A_old_on7"),
                ("移植表 − 新表 (T − B)", "T_tp_on7", "B_h7_on7")]


def transplant(base_p: list, h5: list, h7: list, method: str = "ratio") -> list:
    """base_p にビンごとの面の数の効果を移す。ratio: base × (h7 / h5)、diff: base + (h7 − h5)。[0, 1] に切り、PAV で単調にする。
    h5 が 0 のビンは比が定義できないので比 1 (変えない)。"""
    out = []
    for b, a5, a7 in zip(base_p, h5, h7):
        if method == "ratio":
            v = b * (a7 / a5) if a5 > 0 else b
        else:
            v = b + (a7 - a5)
        out.append(min(1.0, max(0.0, v)))
    mono = pav_decreasing(out, [1.0] * len(out))
    return [round(v, 3) for v in mono]


def predictions_tp(d: pl.DataFrame, old_p: list, method: str = "ratio") -> pl.DataFrame:
    """predictions() に移植表 T を足す。比は外した地点を除く 3 地点の Historical 5 面・7 面の表から求める (旧表は固定)。"""
    pr = predictions(d, old_p)
    parts = []
    for s in SITE_IDS:
        test = pr.filter(pl.col("site_id") == s)
        if test.is_empty():
            continue
        train = d.filter(pl.col("site_id") != s)
        tp = transplant(old_p, fit_row(train, "b5")["p"], fit_row(train, "b7")["p"], method)
        parts.append(test.with_columns(pl.col("b7").map_elements(lambda b, t=tp: t[b], return_dtype=pl.Float64).alias("T_tp_on7")))
    return pl.concat(parts)


def evaluate_transplant() -> str:
    pl.Config.set_tbl_width_chars(260)
    old = json.loads(OLD_TABLE.read_text(encoding="utf-8"))
    old_msm = {t["table_id"]: t for t in old["tables"] if t["model"] == MODEL}
    old_p = [b["p"] for b in old_msm["jma_msm/h0-48"]["bins"]]
    d34 = [b["p"] for b in old_msm["jma_msm/h48-96"]["bins"]]
    d = frame()
    pr = predictions_tp(d, old_p, "ratio")
    pr_diff = predictions_tp(d, old_p, "diff")
    full5, full7 = fit_row(d, "b5")["p"], fit_row(d, "b7")["p"]
    ratio = [round(a7 / a5, 4) if a5 > 0 else 1.0 for a5, a7 in zip(full5, full7)]
    tp_r, tp_d = transplant(old_p, full5, full7, "ratio"), transplant(old_p, full5, full7, "diff")
    d34_r = transplant(d34, full5, full7, "ratio")
    y = pr["y"].to_list()
    scores = pl.DataFrame([{"variant": v, "brier": brier(pr[v].to_list(), y)} for v in ["A_old_on7", "B_h7_on7", "T_tp_on7", "C_old_on5"]]
                          + [{"variant": "T_tp_on7 (diff 方式)", "brier": brier(pr_diff["T_tp_on7"].to_list(), pr_diff["y"].to_list())}])
    ref_old, ref_new, ref_tp = old_p[0], full7[0], tp_r[0]
    rows = pl.DataFrame({"bin": [str(x) for x in mx.BIN_LABELS], "p_h5": full5, "p_h7": full7, "ratio_h7_h5": ratio,
                         "p_old": old_p, "p_tp_ratio": tp_r, "p_tp_diff": tp_d, "p_new_h7": full7,
                         "eff_old": effective(old_p, ref_old), "eff_tp": effective(tp_r, ref_tp), "eff_new": effective(full7, ref_new)})
    d34rows = pl.DataFrame({"bin": [str(x) for x in mx.BIN_LABELS], "p_d34_old": d34, "p_d34_tp": d34_r,
                            "eff_d34_v150": effective(d34, ref_old), "eff_d34_tp_ref_tp": effective(d34_r, ref_tp),
                            "eff_d34_tp_ref_old": effective(d34_r, ref_old)})
    return ("## R12 移植表の評価: 旧表の MSM d1-2 行 × (Historical 7 面 / Historical 5 面) のビンごとの比\n"
            f"標本・方法は r12-msm-retrain-tables.txt と同じ ({d.height} 時間、地点を 1 つずつ外す、日単位ブロックのブートストラップ {N_BOOT} 回、seed {SEED})。\n"
            "移植の比は、外した地点を除く 3 地点の Historical 5 面・7 面の表から毎回求める (旧表は固定)。評価は Historical の予報 = 実質リードの短い予報に対するもの。\n"
            "A = 旧表 × 7 面の入力 (今の本体)、B = 新表 (Historical 7 面) × 7 面、T = 移植表 × 7 面、C = 旧表 × 5 面 (参考)。\n\n"
            "### Brier\n" + fmt(scores, 4)
            + "\n\n### 差 (前 − 後、負 = 前の方が良い)\n" + fmt(contrast_table(pr, TP_CONTRASTS), 4)
            + "\n\n### 地点を外したときの差 (外した地点での ΔBrier)\n" + fmt(fold_table(pr, TP_CONTRASTS), 4)
            + "\n\n### 4 地点全部で作った表の値 (移植表として書き出す値) と実効雲量 (案C)\n"
            + f"基準値: 旧 {ref_old} → 移植 {ref_tp} ({ref_tp - ref_old:+.3f})、新表 {ref_new} ({ref_new - ref_old:+.3f})\n"
            + "p_tp_diff = 差 (h7 − h5) で移した場合 (参考)。移植表は比で作る。\n"
            + fmt(rows, 3)
            + "\n\n### 参考: d3-4 行に同じ比を移した場合 (表は作らない)\n"
            + "eff_d34_tp_ref_tp = 移植した d3-4 行を移植した基準値で正規化、eff_d34_tp_ref_old = 旧基準値で正規化、eff_d34_v150 = 今の値。\n"
            + fmt(d34rows, 3) + "\n")


def build_transplant_table(git: dict, today: date) -> dict:
    """msm7 候補表と同じ組み立てで、MSM d1-2 行だけを移植行に差し替える。"""
    doc = build_new_table(git, today)
    old = json.loads(OLD_TABLE.read_text(encoding="utf-8"))
    base = copy.deepcopy(next(t for t in old["tables"] if t["table_id"] == "jma_msm/h0-48"))
    d = frame()
    full5, full7 = fit_row(d, "b5"), fit_row(d, "b7")
    old_p = [b["p"] for b in base["bins"]]
    tp = transplant(old_p, full5["p"], full7["p"], "ratio")
    ratio = [round(a7 / a5, 4) if a5 > 0 else 1.0 for a5, a7 in zip(full5["p"], full7["p"])]
    new_row = next(t for t in doc["tables"] if t["table_id"] == "jma_msm/h0-48")
    row = base
    row["predictor_definition"] = new_row["predictor_definition"]          # 入力は 7 面 (本体と同じ)
    row["bins"] = [{"label": b["label"], "n": b["n"], "p_old": b["p"], "ratio_h7_h5": r, "p": p, "thin": b["thin"]}
                   for b, r, p in zip(base["bins"], ratio, tp)]
    row["training_source"] = {
        "api": "Single Runs API (absolute level: v1.5.0 jma_msm/h0-48, lead-controlled 0-48h) x Historical Forecast API "
               "(7-level / 5-level ratio per bin)",
        "levels_hpa": LEVELS_7, "n_levels": 7,
        "note": "旧表 + 面の数の効果の移植 (old table with the level-count effect transplanted)"}
    row["normalization"] = {"p_ref": tp[0], "source": "this row bin 0 = v1.5.0 jma_msm/h0-48 bin 0 (0.719) x ratio (Historical 7-level "
                                                     "/ 5-level bin 0), transplanted the same way as the other bins"}
    row["transplant"] = {"method": "ratio", "base": "r1-summit-cloud-sunny 1.0.0 jma_msm/h0-48 (Single Runs, 5 levels)",
                         "ratio_from": "Historical Forecast API jma_msm PAV tables, 7-level / 5-level, 4 foot sites, the R1 training "
                                       "(site, valid_time) set", "p_h5": full5["p"], "p_h7": full7["p"], "ratio_h7_h5": ratio,
                         "monotone": "PAV after multiplying (no merges were needed when built)" if tp == [round(min(1, max(0, v)), 3) for v in
                                     [b * r for b, r in zip(old_p, ratio)]] else "PAV merged some bins"}
    row["status"] = "旧表 + 面の数の効果の移植 (R12 candidate, transplant). docs/r12-msm-transplant.md"
    row["provenance"] = doc["provenance"]
    row["copied_from"] = next(t for t in doc["tables"] if t["table_id"] == "jma_msm/h48-96")["copied_from"]
    doc["tables"] = [row if t["table_id"] == "jma_msm/h0-48" else t for t in doc["tables"]]
    doc["table_set_id"] = "r1-summit-cloud-sunny-msm7-transplant"
    doc["table_set_version"] = "1.1.0"
    doc["description"] = ("R1 with the jma_msm 0-48h row = v1.5.0 row x (Historical 7-level / 5-level) per-bin ratio: keeps the Single "
                          "Runs absolute level (true 0-48h leads) and transplants only the 900/800hPa effect (R12 candidate). ECMWF rows "
                          "and the jma_msm 48-96h row are copied from r1-summit-cloud-sunny 1.0.0 with their original normalization "
                          "(equal to plugin v1.5.0). Needs mos >= 0.2.0.")
    doc.pop("content_sha256", None)
    doc["content_sha256"] = mos.content_sha256(doc)
    return doc


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["fetch", "usage", "eval", "export", "transplant", "export-transplant"])
    ap.add_argument("--out", type=Path)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    if args.cmd == "fetch":
        print(f"fetched {fetch_all()} chunk(s)")
    elif args.cmd == "usage":
        print(usage())
    elif args.cmd == "eval":
        txt = evaluate() + "\n\n" + usage()
        (args.out or OUT_TXT).write_text(txt, encoding="utf-8")
        print(txt)
    elif args.cmd == "transplant":
        txt = evaluate_transplant()
        (args.out or OUT_TP_TXT).write_text(txt, encoding="utf-8")
        print(txt)
    elif args.cmd == "export-transplant":
        doc = build_transplant_table(mx.git_info(), date.today())
        mos.parse_table_set(json.loads(json.dumps(doc, ensure_ascii=False)))
        out = args.out or OUT_TP_TABLE
        out.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print(f"wrote {out} sha256={doc['content_sha256']} commit={doc['provenance']['commit']} dirty={doc['provenance']['dirty']}")
    else:
        doc = build_new_table(mx.git_info(), date.today())
        mos.parse_table_set(json.loads(json.dumps(doc, ensure_ascii=False)))
        out = args.out or OUT_TABLE
        out.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print(f"wrote {out} sha256={doc['content_sha256']} commit={doc['provenance']['commit']} dirty={doc['provenance']['dirty']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
