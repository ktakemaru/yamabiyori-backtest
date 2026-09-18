"""Phase 1 エントリポイント: 取得 → パース → Parquet → サマリ。

    python -m backtest.fetch_previous_runs                 # 5地点 × 2モデル × 過去12ヶ月
    python -m backtest.fetch_previous_runs --start 2026-06-01 --end 2026-06-30 --sites karamatsu
    python -m backtest.fetch_previous_runs --skip-fetch    # 生JSONから Parquet を作り直すだけ
"""
import argparse
import logging
from datetime import date
from pathlib import Path

import polars as pl

from . import config
from . import openmeteo_previous_runs as om
from . import parse

log = logging.getLogger("backtest")


def _uniq(df: pl.DataFrame, col: str) -> list:
    return df.select(pl.col(col).unique().sort()).to_series().to_list()


def summarize(df: pl.DataFrame) -> str:
    """行数・期間・欠損率 (model × variable × lead) を人が読める表にする。"""
    if df.is_empty():
        return "(empty)"
    lines = [f"rows: {df.height:,}",
             f"sites: {_uniq(df, 'site_id')}",
             f"models: {_uniq(df, 'model')}"]
    span = df.group_by("model").agg(pl.col("valid_time").min().alias("first"),
                                    pl.col("valid_time").max().alias("last"),
                                    pl.col("valid_time").n_unique().alias("hours")).sort("model")
    lines.append("period (UTC) by model:")
    for r in span.iter_rows(named=True):
        lines.append(f"  {r['model']:14s} {r['first']} .. {r['last']}  ({r['hours']:,} distinct hours)")
    miss = (df.group_by(["model", "variable", "lead_days"])
              .agg(pl.col("value").is_null().mean().alias("missing"), pl.len().alias("n"))
              .sort(["model", "variable", "lead_days"]))
    lines.append("missing rate by model / variable / lead (n = rows per variable):")
    for model in _uniq(miss, "model"):
        lines.append(f"  [{model}]")
        sub = miss.filter(pl.col("model") == model)
        for variable in _uniq(sub, "variable"):
            row = sub.filter(pl.col("variable") == variable).sort("lead_days")
            cells = " ".join(f"d{r['lead_days']}={r['missing']*100:5.1f}%" for r in row.iter_rows(named=True))
            n = row.select(pl.col("n").sum()).item()
            lines.append(f"    {variable:22s} {cells}  (n={n:,})")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    start_default, end_default = config.default_period()
    ap.add_argument("--start", type=date.fromisoformat, default=start_default)
    ap.add_argument("--end", type=date.fromisoformat, default=end_default)
    ap.add_argument("--sites", nargs="*", default=None, help="site_id (省略時は config.SITES 全て)")
    ap.add_argument("--models", nargs="*", default=config.MODELS)
    ap.add_argument("--raw-dir", type=Path, default=config.RAW_DIR)
    ap.add_argument("--out", type=Path, default=config.PARQUET_PATH)
    ap.add_argument("--skip-fetch", action="store_true", help="取得せず生JSONからParquetを再生成")
    ap.add_argument("--sleep", type=float, default=config.SLEEP_SECONDS)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    sites = config.SITES if args.sites is None else [s for s in config.SITES if s["site_id"] in args.sites]
    unknown = set(args.sites or []) - {s["site_id"] for s in sites}
    if unknown:
        ap.error(f"unknown site_id: {sorted(unknown)}")

    failures = []
    if not args.skip_fetch:
        log.info("fetch %s .. %s, sites=%s, models=%s", args.start, args.end,
                 [s["site_id"] for s in sites], args.models)
        _, failures = om.fetch_all(sites, args.models, args.start, args.end, raw_dir=args.raw_dir,
                                   sleep_seconds=args.sleep)

    log.info("parsing raw files under %s", args.raw_dir)
    df = parse.parse_raw_dir(args.raw_dir)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    df.write_parquet(args.out, compression="zstd")
    log.info("wrote %s (%d rows)", args.out, df.height)
    print()
    print(summarize(df))
    if failures:
        print(f"\n!! {len(failures)} chunk(s) not fetched -- re-run the same command to retry:")
        for f in failures:
            print("  ", f)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
