"""Parquet の中身を確認するための簡易チェック (Phase 1 の受け入れ確認用)。

    python -m backtest.quicklook [--parquet data/parquet/forecast_long.parquet]

1. 値域チェック (雲量/湿度 0-100、降水・風速 >= 0、気温の極端値)
2. 時刻の重複・欠落
3. 「リード N 日 vs リード 1 日」の気温 MAE — 実況ではなく最新に近い予報を基準にした
   予報の自己一貫性 (run-to-run consistency)。実況との MAE は Phase 2 で観測が入ってから。
"""
import argparse
from pathlib import Path

import polars as pl

from . import config

RANGES = {
    "cloud_cover": (0, 100), "cloud_cover_low": (0, 100), "cloud_cover_mid": (0, 100), "cloud_cover_high": (0, 100),
    "relative_humidity_2m": (0, 100), "precipitation": (0, 200), "wind_speed_10m": (0, 80),
    "wind_gusts_10m": (0, 100), "temperature_2m": (-45, 45),
}


def range_check(df: pl.DataFrame) -> pl.DataFrame:
    rows = []
    for var, (lo, hi) in RANGES.items():
        sub = df.filter(pl.col("variable") == var)
        stats = sub.select(
            pl.col("value").min().alias("min"), pl.col("value").max().alias("max"),
            ((pl.col("value") < lo) | (pl.col("value") > hi)).sum().alias("out_of_range"),
            pl.col("value").is_null().sum().alias("nulls"), pl.len().alias("n"),
        ).row(0, named=True)
        rows.append({"variable": var, **stats})
    return pl.DataFrame(rows)


def time_check(df: pl.DataFrame) -> pl.DataFrame:
    """(site, model, variable, lead) ごとに、時刻の重複と 1 時間刻みでない箇所を数える。"""
    return (df.sort("valid_time")
              .group_by(["site_id", "model", "variable", "lead_days"])
              .agg(
                  (pl.len() - pl.col("valid_time").n_unique()).alias("duplicates"),
                  (pl.col("valid_time").diff().dt.total_minutes().drop_nulls() != 60).sum().alias("gaps"),
              )
              .filter((pl.col("duplicates") > 0) | (pl.col("gaps") > 0)))


def mae_vs_lead1(df: pl.DataFrame, variable: str = "temperature_2m", model: str = "ecmwf_ifs025",
                 site_id: str = None) -> pl.DataFrame:
    sub = df.filter((pl.col("variable") == variable) & (pl.col("model") == model))
    if site_id:
        sub = sub.filter(pl.col("site_id") == site_id)
    ref = (sub.filter(pl.col("lead_days") == 1)
              .select("site_id", "valid_time", pl.col("value").alias("ref")))
    joined = sub.join(ref, on=["site_id", "valid_time"], how="inner").drop_nulls(["value", "ref"])
    return (joined.group_by("lead_days")
                  .agg((pl.col("value") - pl.col("ref")).abs().mean().alias("mae_vs_lead1"),
                       (pl.col("value") - pl.col("ref")).mean().alias("bias_vs_lead1"),
                       pl.len().alias("n"))
                  .sort("lead_days"))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--parquet", type=Path, default=config.PARQUET_PATH)
    args = ap.parse_args(argv)
    df = pl.read_parquet(args.parquet)
    pl.Config.set_tbl_rows(60)
    print(f"{args.parquet}: {df.height:,} rows\n")
    print("## 値域チェック")
    print(range_check(df))
    print("\n## 時刻の重複・欠落 (問題のある系列だけ表示。空なら OK)")
    print(time_check(df))
    print("\n## 気温 MAE: リード N 日 vs リード 1 日 (ecmwf_ifs025、5地点プール、°C)")
    print("   ※ 実況との比較ではない (Phase 2 で観測を入れてから)。予報の自己一貫性の目安。")
    print(mae_vs_lead1(df))
    print("\n## 同上、地点別 (lead 7 のみ)")
    per_site = pl.concat([mae_vs_lead1(df, site_id=s["site_id"]).with_columns(pl.lit(s["site_id"]).alias("site_id"))
                          for s in config.SITES]).filter(pl.col("lead_days") == 7)
    print(per_site.select("site_id", "mae_vs_lead1", "bias_vs_lead1", "n"))
    print("\n## jma_msm: lead 2 vs lead 1 (気温)")
    print(mae_vs_lead1(df, model="jma_msm"))


if __name__ == "__main__":
    main()
