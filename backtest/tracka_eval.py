"""Track A (Previous Runs, 通年) の地上全雲量で、季節依存を確認する。

Track B (Single Runs) は ecmwf_ifs025 が 2026-04-02 以降しか無く暖候期のみ。Track A の `forecast_long.parquet`
(2025-09-18〜2026-09-17, `cloud_cover_previous_dayN`) は 1 年分あるので、地上全雲量に限って
lead 別 × 季節別 (暖候期 4〜9 月 / 寒候期 10〜3 月) の AUC / PSS_max を出す。実況は observation_long の sun1h
(etrn キャッシュ: 2026-01-15〜02-28, 06-11〜09-14; アメダス map: 2026-09-11〜)。

    python -m backtest.tracka_eval
"""
import argparse
from pathlib import Path

import polars as pl

from . import config, skill
from .trackb_eval import DAY_HOURS_JST, SITE_BY_STATION, SUN_SUNNY_H, fmt, obs_wide

FORECAST_LONG = config.PARQUET_PATH
OBS_LONG = config.DATA_DIR / "parquet" / "observation_long.parquet"
WARM_MONTHS = [4, 5, 6, 7, 8, 9]


def pairs(fc: pl.DataFrame, obs: pl.DataFrame, variable: str = "cloud_cover") -> pl.DataFrame:
    """(pair, model, lead_days, valid_time, value_h1 = T-1h/T 平均, obs_sunny, season)。"""
    parts = []
    for station, site in SITE_BY_STATION.items():
        o = obs_wide(obs, station, ["sun1h"])
        if o.is_empty():
            continue
        f = fc.filter((pl.col("site_id") == site["site_id"]) & (pl.col("variable") == variable) & pl.col("value").is_not_null())
        if f.is_empty():
            continue
        keys = ["site_id", "model", "lead_days"]
        f = f.sort(keys + ["valid_time"])
        prev = f.with_columns((pl.col("valid_time") + pl.duration(hours=1)).alias("valid_time"), pl.col("value").alias("value_prev")) \
                .select(keys + ["valid_time", "value_prev"])
        f = f.join(prev, on=keys + ["valid_time"], how="left").with_columns(((pl.col("value") + pl.col("value_prev")) / 2).alias("value_h1"))
        j = f.join(o, on="valid_time", how="inner")
        j = j.with_columns(pl.col("valid_time").dt.convert_time_zone("Asia/Tokyo").dt.hour().alias("hour_jst"),
                           pl.col("valid_time").dt.convert_time_zone("Asia/Tokyo").dt.month().alias("month_jst"))
        j = j.filter(pl.col("hour_jst").is_in(DAY_HOURS_JST) & pl.col("value_h1").is_not_null())
        j = j.with_columns((pl.col("sun1h") >= SUN_SUNNY_H).alias("obs_sunny"),
                           pl.when(pl.col("month_jst").is_in(WARM_MONTHS)).then(pl.lit("warm(4-9)")).otherwise(pl.lit("cold(10-3)")).alias("season"),
                           pl.lit(f"{site['site_id']}/{station}").alias("pair"))
        parts.append(j.select("pair", "model", "lead_days", "valid_time", "season", "value_h1", "obs_sunny"))
    return pl.concat(parts) if parts else pl.DataFrame()


def skill_table(d: pl.DataFrame, keys: list[str]) -> pl.DataFrame:
    rows = []
    for key, g in d.group_by(keys, maintain_order=True):
        cloud = g["value_h1"].to_list(); lab = g["obs_sunny"].to_list(); sc = [-c for c in cloud]
        a, se, n1, n0 = skill.auc(sc, lab)
        pm, th = skill.pss_max(sc, lab)
        rows.append({**dict(zip(keys, key)), "n": len(lab), "n_sunny": n1, "obs_sunny_rate": n1 / len(lab) if lab else None,
                     "auc": a, "auc_se": se, "auc_ci95_lo": (a - 1.96 * se) if a is not None else None,
                     "pss_max": pm, "cloud_th_at_pss_max": (-th) if th is not None else None})
    return pl.DataFrame(rows).sort(keys)


def report(fc_path: Path = FORECAST_LONG, obs_path: Path = OBS_LONG) -> dict:
    fc = pl.read_parquet(fc_path)
    obs = pl.read_parquet(obs_path)
    d = pairs(fc, obs)
    d_foot = d.filter(~pl.col("pair").str.starts_with("fuji"))
    d_fuji = d.filter(pl.col("pair").str.starts_with("fuji"))
    out = {
        "all_year": skill_table(d_foot, ["model", "lead_days"]),
        "by_season": skill_table(d_foot, ["model", "season", "lead_days"]),
        "by_season_pair": skill_table(d_foot, ["pair", "model", "season", "lead_days"]),
        "fuji_summit": skill_table(d_fuji, ["model", "season", "lead_days"]) if not d_fuji.is_empty() else pl.DataFrame(),
        "months": d.group_by("season", "pair").agg(pl.col("valid_time").min().alias("first"), pl.col("valid_time").max().alias("last"), pl.len().alias("n")).sort("season", "pair"),
    }
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)
    rep = report()
    cols = ["model", "lead_days", "n", "obs_sunny_rate", "auc", "auc_ci95_lo", "pss_max", "cloud_th_at_pss_max"]
    text = ("## E-0 標本の期間 (season × pair)\n" + fmt(rep["months"])
            + "\n\n## E-1 Track A 地上全雲量 (previous_dayN) vs 麓日照: 通年, 麓 4 地点プール\n" + fmt(rep["all_year"].select(cols))
            + "\n\n## E-2 同上 季節別 (warm=4-9月, cold=10-3月)\n" + fmt(rep["by_season"].select(["season"] + cols))
            + "\n\n## E-3 同上 地点 × 季節別\n" + fmt(rep["by_season_pair"].select(["pair", "season"] + cols))
            + "\n\n## E-4 富士山頂 (地上全雲量 vs 山頂日照; 夏季のみ)\n" + (fmt(rep["fuji_summit"].select(["season"] + cols)) if not rep["fuji_summit"].is_empty() else "(no data)"))
    print(text)
    if args.out:
        args.out.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
