"""R12: R1 の jma_msm 行は 900/800hPa 無しの山頂雲量で学習された (Single Runs では MSM の 900/800 が全 null; R5) が、
本体 v1.5.0 は Forecast API で 900/800 も使って山頂雲量を作る。その違いで、R1 で引くビンと実効雲量がどれだけ動くかを標高別に測る。

入力: winter_r1 で取得した Historical Forecast API の jma_msm (1000〜600hPa の 7 面すべて、3 地点 × 冬 2026-01-14〜02-28 と夏 06-11〜09-17)。
同じ時刻の同じ鉛直分布を、目標標高 600〜3800m (100m 刻み) へ 2 通りに内挿する (本体と同じ summit_interp):
  with    = 使える面すべて (本体の Forecast API と同じ)
  without = 900/800hPa を除く (R1 学習時の Single Runs と同じ)
3 地点の分布を「鉛直構造の標本」として使い、目標標高はその地点の山頂とは限らない (本体 76 座の標高帯に当てはめるための近似)。
毎時の瞬時値 (本体は毎時値に表を当てる)。行 (lead 群) は面の数に依存しないので、ビンと実効雲量だけが変わる。

    python -m backtest.r12_msm_levels [--out docs/r12-msm-levels.txt]
"""
import argparse
import json
from pathlib import Path

import polars as pl

import mos

from . import config
from . import summit_interp as si
from .trackb_eval import DAY_HOURS_JST, fmt
from .winter_r1 import LEVELS_NO_900_800, TABLE_PATH, load_site_hourly

OUT = config.DATA_DIR.parent / "docs" / "r12-msm-levels.txt"
TARGETS_M = list(range(600, 3801, 100))
BANDS_M = [(500, 999), (1000, 1499), (1500, 1999), (2000, 2499), (2500, 2999), (3000, 3499), (3500, 3999)]
# 本体 v1.5.0 (52d3d2a) の MOUNTAINS 76 座の elevation_m を 500m 帯で数えたもの (git show で読み取りのみ、2026-09-23)
BODY_SUMMITS_PER_BAND = {(500, 999): 5, (1000, 1499): 10, (1500, 1999): 18, (2000, 2499): 15, (2500, 2999): 21, (3000, 3499): 6,
                         (3500, 3999): 1}


def frame(ts) -> pl.DataFrame:
    rows_tables = {name: mos.select_table(ts, predictor="cloud_cover_at_summit", target="p_sunny", model="jma_msm",
                                          lead_hours=lh, hour_jst=12, month=7).table for name, lh in (("h0_48", 0), ("h48_96", 60))}
    rows = []
    for site_id, h in load_site_hourly("jma_msm").items():
        hours_jst = [(int(t[11:13]) + 9) % 24 for t in h["time"]]
        months = [int(t[5:7]) for t in h["time"]]
        for z in TARGETS_M:
            w = si.series_at_altitude(h, "cloudcover", z)
            wo = si.series_at_altitude(h, "cloudcover", z, LEVELS_NO_900_800)
            for i, (a, b) in enumerate(zip(w, wo)):
                if a is None or b is None:
                    continue
                season = "winter" if months[i] in (1, 2) else "summer"
                rec = [site_id, z, season, hours_jst[i] in DAY_HOURS_JST, a, b, mos.bin_index(ts.edges, a) != mos.bin_index(ts.edges, b)]
                for t in rows_tables.values():
                    rec.append(mos.effective_value(ts, t, a) - mos.effective_value(ts, t, b))
                rows.append(rec)
    return pl.DataFrame(rows, schema=["site_id", "target_m", "season", "daytime", "cloud_with", "cloud_without", "bin_differs",
                                      "d_eff_h0_48", "d_eff_h48_96"], orient="row")


def gph_levels() -> pl.DataFrame:
    rows = []
    for site_id, h in load_site_hourly("jma_msm").items():
        for lv in (900, 800):
            zs = [z for z in h.get(f"geopotential_height_{lv}hPa", []) if z is not None]
            rows.append({"site_id": site_id, "level_hpa": lv, "n": len(zs), "gph_mean_m": sum(zs) / len(zs),
                         "gph_min_m": min(zs), "gph_max_m": max(zs)})
    return pl.DataFrame(rows)


def summarize(d: pl.DataFrame, keys: list[str]) -> pl.DataFrame:
    dr = pl.col("cloud_with") - pl.col("cloud_without")
    e = pl.col("d_eff_h0_48")
    return (d.group_by(keys).agg(pl.len().alias("n_hours"), dr.abs().mean().alias("mean_abs_d_cloud"), dr.mean().alias("mean_d_cloud"),
                                 pl.col("bin_differs").mean().alias("share_bin_differs"),
                                 e.abs().mean().alias("mean_abs_d_eff_h0_48"), e.abs().quantile(0.9).alias("p90_abs_d_eff_h0_48"),
                                 (e.abs() >= 10).mean().alias("share_d_eff_ge10"), e.mean().alias("mean_d_eff_h0_48"),
                                 pl.col("d_eff_h48_96").abs().mean().alias("mean_abs_d_eff_h48_96"))
            .sort(keys))


def band_expr() -> pl.Expr:
    e = pl.lit(None, dtype=pl.String)
    for lo, hi in BANDS_M:
        e = pl.when(pl.col("target_m").is_between(lo, hi)).then(pl.lit(f"{lo:04d}-{hi:04d}")).otherwise(e)
    return e.alias("band_m")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args(argv)
    pl.Config.set_tbl_width_chars(260)
    ts = mos.parse_table_set(json.loads(TABLE_PATH.read_text(encoding="utf-8")))
    d = frame(ts).with_columns(band_expr())
    day = d.filter(pl.col("daytime"))
    counts = pl.DataFrame({"band_m": [f"{lo:04d}-{hi:04d}" for lo, hi in BANDS_M], "body_summits": [BODY_SUMMITS_PER_BAND[b] for b in BANDS_M]})
    by_band = summarize(day, ["band_m"]).join(counts, on="band_m", how="left").sort("band_m")
    txt = ("## R12 jma_msm の山頂雲量: 900/800hPa あり (本体) となし (R1 学習) の差\n"
           "入力: Historical Forecast API の jma_msm 7 面 (3 地点, 冬 2026-01-15〜02-28 + 夏 2026-06-12〜09-17 前後)。毎時の瞬時値。\n"
           "d_cloud = 山頂雲量 (あり − なし)、bin_differs = R1 のビンが変わる割合、d_eff = 実効雲量 (案C) の差 (あり − なし、正 = 本体の方が曇り寄り)。\n"
           "h0_48 = msm d1-2 行、h48_96 = msm d3-4 行。目標標高は 3 地点の鉛直分布へ当てた格子で、山頂そのものではない。\n\n"
           "### 900/800hPa のジオポテンシャル高度 (この標本)\n" + fmt(gph_levels(), 0)
           + "\n\n### 日中 (07-17 時)、標高帯別 (body_summits = 本体 76 座のうちその帯の山の数)\n" + fmt(by_band, 3)
           + "\n\n### 日中、標高 100m ごと\n" + fmt(summarize(day, ["target_m"]), 3)
           + "\n\n### 日中、標高帯 × 季節\n" + fmt(summarize(day.filter(pl.col("band_m").is_not_null()), ["band_m", "season"]), 3)
           + "\n\n### 全時刻 (夜間を含む)、標高帯別\n" + fmt(summarize(d, ["band_m"]), 3) + "\n")
    args.out.write_text(txt, encoding="utf-8")
    print(txt)


if __name__ == "__main__":
    main()
