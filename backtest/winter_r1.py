"""冬季に R1 (山頂雲量 → P(晴れ) の較正表) が害になるかの暫定検証 (2026-09-23)。

Single Runs は ecmwf_ifs025 が 2026-04-02〜、jma_msm が 2026-05-13〜 しか無く (api-findings §2.1)、2026-01〜02 の冬をリード別に再現できない。
代わりに Historical Forecast API (最新の予報を継ぎ足した系列 = 実質のリードは短い; lead は指定できない) の気圧面から、
本体と同じ内挿 (summit_interp) で山頂雲量を作り、旧ハーネスの etrn キャッシュ (麓の日照 sun1h) と突き合わせる。

- 地点: 冬の etrn がある麓ペア 3 組 (唐松岳/白馬、八ヶ岳赤岳/野辺山、日光白根山/奥日光)。安達太良山 (鷲倉) は冬の実況が無い。
- 期間: 冬 2026-01-15〜02-28 (etrn キャッシュの範囲)。対照として同じ API の夏 2026-06-12〜09-17 (R1 の学習期間と同じ実況 = 表にとって標本内)。
- 予報値: 学習と同じ value_h1 (T-1h と T の平均)、日中 = JST 07〜17 時に終わる 1 時間 (trackb_eval.DAY_HOURS_JST)、晴れ = sun1h >= 0.4h。
  感度として 09〜15 時 (冬でも 1 時間まるごと日が出ている時間) も出す。
- 比べるもの: 生値を確率と読む P = 1 − 雲量/100 と、R1 の d1-2 行 (lead 0〜48h) の P。Brier・信頼度表・日単位ブロックのブートストラップ。
  jma_msm は本体 (Forecast API) と同じく 900/800hPa も使う内挿を主とし、R1 の学習 (Single Runs: 900/800 が null) と同じ面だけの内挿を感度として出す。

    python -m backtest.winter_r1 fetch        # 生 JSON を data/raw/historical_forecast/ に保存 (保存済みはスキップ、sleep あり)
    python -m backtest.winter_r1 eval         # → docs/r1-winter-provisional-tables.txt
"""
import argparse
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
from . import summit_interp as si
from .obs_import import PARQUET_PATH as OBS_PATH
from .trackb_eval import DAY_HOURS_JST, SUN_SUNNY_H, fmt, obs_wide

log = logging.getLogger("backtest")

HISTORICAL_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
RAW_DIR = config.DATA_DIR / "raw" / "historical_forecast"
OUT = config.DATA_DIR.parent / "docs" / "r1-winter-provisional-tables.txt"
TABLE_PATH = config.DATA_DIR.parent / "mos_tables" / "r1-summit-cloud-sunny.json"
MODELS = ["ecmwf_ifs025", "jma_msm"]
SITE_IDS = ["karamatsu", "akadake", "nikko_shirane"]
PERIODS = {"winter": (date(2026, 1, 14), date(2026, 2, 28)), "summer": (date(2026, 6, 11), date(2026, 9, 17))}
EVAL_DATES_JST = {"winter": (date(2026, 1, 15), date(2026, 2, 28)), "summer": (date(2026, 6, 12), date(2026, 9, 17))}
CORE_HOURS_JST = list(range(9, 16))
LEVELS_NO_900_800 = [1000, 925, 850, 700, 600]
SLEEP_SECONDS = 3.0
MAX_RETRIES = 6
N_BOOT = 2000
SEED = 20260923
SITES = [s for s in config.SITES if s["site_id"] in SITE_IDS]


def hourly_vars() -> list[str]:
    return [f"{k}_{lv}hPa" for lv in si.LEVEL_STACK_HPA for k in ("cloud_cover", "relative_humidity", "geopotential_height")]


def month_chunks(start: date, end: date) -> list[tuple[date, date]]:
    out, d = [], start
    while d <= end:
        nxt = (d.replace(day=1) + timedelta(days=32)).replace(day=1)
        out.append((d, min(end, nxt - timedelta(days=1))))
        d = nxt
    return out


def raw_path(model: str, a: date, b: date) -> Path:
    return RAW_DIR / model / f"{a:%Y-%m-%d}_{b:%Y-%m-%d}.json.gz"


def fetch_chunk(session: requests.Session, model: str, a: date, b: date) -> bool:
    path = raw_path(model, a, b)
    if path.exists():
        return False
    params = {"latitude": ",".join(str(s["lat"]) for s in SITES), "longitude": ",".join(str(s["lon"]) for s in SITES),
              "models": model, "hourly": ",".join(hourly_vars()), "start_date": a.isoformat(), "end_date": b.isoformat(),
              "timezone": "UTC"}
    delay = 10.0
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = session.get(HISTORICAL_URL, params=params, timeout=300)
            if r.status_code == 200:
                body = r.json()
                break
            if r.status_code == 400:
                raise RuntimeError(f"HTTP 400 {r.text[:200]}")
            log.warning("HTTP %s (attempt %d/%d): %s", r.status_code, attempt, MAX_RETRIES, r.text[:120])
        except (requests.RequestException, ValueError) as e:
            log.warning("request failed (attempt %d/%d): %s", attempt, MAX_RETRIES, e)
        time.sleep(delay)
        delay = min(delay * 1.5, 60)
    else:
        raise RuntimeError(f"gave up: {model} {a}..{b}")
    envelope = {"model": model, "start_date": a.isoformat(), "end_date": b.isoformat(), "sites": [s["site_id"] for s in SITES],
                "site_defs": SITES, "request": params, "url": r.url, "status": r.status_code,
                "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "body": body}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump(envelope, f, ensure_ascii=False)
    tmp.replace(path)
    log.info("fetched %s %s..%s", model, a, b)
    return True


def fetch_all() -> int:
    session = requests.Session()
    session.headers["User-Agent"] = "yamabiyori-backtest (winter R1 check)"
    n = 0
    for model in MODELS:
        for a0, b0 in PERIODS.values():
            for a, b in month_chunks(a0, b0):
                if fetch_chunk(session, model, a, b):
                    n += 1
                    time.sleep(SLEEP_SECONDS)
    return n


# ---------------------------------------------------------------- 評価
def load_site_hourly(model: str) -> dict:
    """site_id -> 本体命名の hourly (チャンクを時刻順に連結)。"""
    by_site = {s["site_id"]: {} for s in SITES}
    for path in sorted((RAW_DIR / model).glob("*.json.gz")):
        with gzip.open(path, "rt", encoding="utf-8") as f:
            env = json.load(f)
        for site_id, loc in zip(env["sites"], env["body"]):
            h = si.to_core_naming(loc["hourly"])
            for i, t in enumerate(h["time"]):
                by_site[site_id][t] = {k: v[i] for k, v in h.items() if k != "time"}
    out = {}
    for site_id, rows in by_site.items():
        times = sorted(rows)
        keys = sorted({k for r in rows.values() for k in r})
        out[site_id] = {"time": times, **{k: [rows[t].get(k) for t in times] for k in keys}}
    return out


def forecast_frame(model: str) -> pl.DataFrame:
    """(site, valid_time UTC, value_h1) を内挿 2 通り (all / no900800) で。"""
    rows = []
    sites = {s["site_id"]: s for s in SITES}
    for site_id, h in load_site_hourly(model).items():
        elev = sites[site_id]["elevation_m"]
        variants = {"all_levels": si.series_at_altitude(h, "cloudcover", elev),
                    "no_900_800": si.series_at_altitude(h, "cloudcover", elev, LEVELS_NO_900_800)}
        times = [datetime.fromisoformat(t).replace(tzinfo=timezone.utc) for t in h["time"]]
        for variant, v in variants.items():
            for i in range(1, len(times)):
                if times[i] - times[i - 1] != timedelta(hours=1) or v[i] is None or v[i - 1] is None:
                    continue
                rows.append((site_id, model, variant, times[i], (v[i] + v[i - 1]) / 2))
    return pl.DataFrame(rows, schema={"site_id": pl.String, "model": pl.String, "variant": pl.String,
                                      "valid_time": pl.Datetime("us", "UTC"), "value_h1": pl.Float64}, orient="row")


def pairs(fc: pl.DataFrame, obs: pl.DataFrame) -> pl.DataFrame:
    parts = []
    for s in SITES:
        o = obs_wide(obs, s["station_id"], ["sun1h"])
        f = fc.filter(pl.col("site_id") == s["site_id"])
        parts.append(f.join(o, on="valid_time", how="inner"))
    d = pl.concat(parts)
    jst = pl.col("valid_time").dt.convert_time_zone("Asia/Tokyo")
    d = d.with_columns(jst.dt.hour().alias("hour_jst"), jst.dt.date().alias("date_jst"),
                       (pl.col("sun1h") >= SUN_SUNNY_H).cast(pl.Int8).alias("y"))
    season = pl.lit(None, dtype=pl.String)
    for name, (a, b) in EVAL_DATES_JST.items():
        season = pl.when(pl.col("date_jst").is_between(a, b)).then(pl.lit(name)).otherwise(season)
    return d.with_columns(season.alias("season")).filter(pl.col("season").is_not_null() & pl.col("hour_jst").is_in(DAY_HOURS_JST))


def add_probs(d: pl.DataFrame, ts) -> pl.DataFrame:
    tables = {m: mos.select_table(ts, predictor="cloud_cover_at_summit", target="p_sunny", model=m, lead_hours=0,
                                  hour_jst=12, month=7).table for m in MODELS}
    r1 = [mos.probability(ts, tables[m], v) for m, v in zip(d["model"], d["value_h1"])]
    return d.with_columns((1 - pl.col("value_h1").clip(0, 100) / 100).alias("p_raw"), pl.Series("p_r1", r1, dtype=pl.Float64))


def bootstrap_diff(g: pl.DataFrame, n_boot: int = N_BOOT, seed: int = SEED) -> tuple[float, float, float]:
    """ΔBrier = Brier(R1) − Brier(生値) の日単位ブロック・ブートストラップ (95% 区間, Δ<0 の割合)。"""
    per_day = (g.with_columns(((pl.col("p_r1") - pl.col("y")) ** 2 - (pl.col("p_raw") - pl.col("y")) ** 2).alias("dsq"))
               .group_by("date_jst").agg(pl.col("dsq").sum(), pl.len().alias("n")))
    dsq, n = per_day["dsq"].to_list(), per_day["n"].to_list()
    rng = random.Random(seed)
    k = len(dsq)
    stats = []
    for _ in range(n_boot):
        idx = [rng.randrange(k) for _ in range(k)]
        stats.append(sum(dsq[i] for i in idx) / sum(n[i] for i in idx))
    stats.sort()
    return stats[int(0.025 * n_boot)], stats[int(0.975 * n_boot) - 1], sum(1 for x in stats if x < 0) / n_boot


def score_table(d: pl.DataFrame, keys: list[str]) -> pl.DataFrame:
    rows = []
    for key, g in d.group_by(keys, maintain_order=True):
        base = g["y"].mean()
        b_raw = ((g["p_raw"] - g["y"]) ** 2).mean()
        b_r1 = ((g["p_r1"] - g["y"]) ** 2).mean()
        lo, hi, p_neg = bootstrap_diff(g)
        rows.append({**dict(zip(keys, key)), "n_hours": g.height, "n_days": g["date_jst"].n_unique(), "sunny_rate": base,
                     "share_cloud0": (g["value_h1"] <= 0).mean(), "share_cloud_gt50": (g["value_h1"] > 50).mean(),
                     "brier_clim": base * (1 - base), "brier_raw": b_raw, "brier_r1": b_r1, "d_brier": b_r1 - b_raw,
                     "d_ci95_lo": lo, "d_ci95_hi": hi, "p_boot_r1_better": p_neg})
    return pl.DataFrame(rows).sort(keys)


def reliability(d: pl.DataFrame, keys: list[str]) -> tuple[pl.DataFrame, pl.DataFrame]:
    r1 = (d.group_by(keys + ["p_r1"]).agg(pl.len().alias("n"), pl.col("y").mean().alias("obs_sunny"))
          .rename({"p_r1": "p_forecast"}).sort(keys + ["p_forecast"]))
    rb = (pl.when(pl.col("p_raw") >= 1.0).then(pl.lit("=1.0 (cloud 0%)"))
          .otherwise((pl.col("p_raw") * 10).floor().cast(pl.Int32).cast(pl.String).str.zfill(2)))
    raw = (d.with_columns(rb.alias("p_bin")).group_by(keys + ["p_bin"])
           .agg(pl.len().alias("n"), pl.col("p_raw").mean().alias("p_forecast_mean"), pl.col("y").mean().alias("obs_sunny"))
           .sort(keys + ["p_bin"]))
    return r1, raw


def evaluate() -> str:
    pl.Config.set_tbl_width_chars(260)
    ts = mos.parse_table_set(json.loads(TABLE_PATH.read_text(encoding="utf-8")))
    fc = pl.concat([forecast_frame(m) for m in MODELS])
    obs = pl.read_parquet(OBS_PATH)
    d = add_probs(pairs(fc, obs), ts)
    main = d.filter(pl.col("variant") == "all_levels")
    core = main.filter(pl.col("hour_jst").is_in(CORE_HOURS_JST))
    msm_sens = d.filter((pl.col("model") == "jma_msm") & (pl.col("variant") == "no_900_800"))
    rel_r1, rel_raw = reliability(main, ["season", "model"])
    keys = ["season", "model"]
    txt = ("## R1 冬季の暫定検証 (Historical Forecast API; 短いリードのみ)\n"
           "予報: Historical Forecast API (最新の予報の継ぎ足し、lead 指定不可) の気圧面 → 本体と同じ山頂内挿、value_h1 (T-1h/T 平均)。\n"
           "実況: 麓の sun1h >= 0.4h (etrn_cache 優先)。地点: karamatsu/白馬, akadake/野辺山, nikko_shirane/奥日光。\n"
           "R1 = d1-2 行 (lead 0-48h) の P(晴れ)。生値 = 1 − 雲量/100。d_brier = Brier(R1) − Brier(生値) (負 = R1 が良い)。\n"
           f"95% 区間と p_boot_r1_better は日単位ブロックのブートストラップ ({N_BOOT} 回, seed {SEED})。\n"
           "summer は R1 の学習と同じ期間・同じ実況 (表にとって標本内) なので、冬との比較の対照 (上限側) として読む。\n\n"
           "### 主: 日中 07-17 時 (学習と同じ定義)、900/800hPa も使う内挿 (本体の Forecast API と同じ)\n" + fmt(score_table(main, keys), 3)
           + "\n\n### 感度 1: 09-15 時だけ (冬でも 1 時間まるごと日が出ている時間)\n" + fmt(score_table(core, keys), 3)
           + "\n\n### 感度 2: jma_msm を R1 の学習と同じ面 (900/800hPa 無し) で内挿\n" + fmt(score_table(msm_sens, keys), 3)
           + "\n\n### 地点別 (主)\n" + fmt(score_table(main, ["season", "model", "site_id"]), 3)
           + "\n\n### 信頼度表: R1 (予報 P ごとの実況の晴れ率)\n" + fmt(rel_r1, 3)
           + "\n\n### 信頼度表: 生値 (1 − 雲量/100 を 0.1 刻み、=1.0 は雲量 0%)\n" + fmt(rel_raw, 3) + "\n")
    return txt


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["fetch", "eval"])
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    if args.cmd == "fetch":
        print(f"fetched {fetch_all()} chunk(s)")
        return 0
    txt = evaluate()
    args.out.write_text(txt, encoding="utf-8")
    print(txt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
