"""Track B エントリポイント: Single Runs API の気圧面データを取得 (生JSON保存まで)。

    python -m backtest.fetch_single_runs --start 2026-06-11 --end 2026-09-17            # 00Z と 12Z
    python -m backtest.fetch_single_runs --start 2026-06-11 --end 2026-09-17 --hours 0  # 00Z のみ
    python -m backtest.fetch_single_runs --models ecmwf_ifs025 --start 2026-09-01 --end 2026-09-17
"""
import argparse
import logging
from datetime import date
from pathlib import Path

from . import config
from . import openmeteo_single_runs as osr

log = logging.getLogger("backtest")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", type=date.fromisoformat, required=True)
    ap.add_argument("--end", type=date.fromisoformat, required=True)
    ap.add_argument("--models", nargs="*", default=config.MODELS)
    ap.add_argument("--hours", nargs="*", type=int, default=osr.RUN_HOURS, help="ラン初期時刻 (UTC)")
    ap.add_argument("--raw-dir", type=Path, default=osr.RAW_DIR)
    ap.add_argument("--sleep", type=float, default=osr.SLEEP_SECONDS)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    fetched, failures = osr.fetch_all(args.models, args.start, args.end, hours=args.hours,
                                      raw_dir=args.raw_dir, sleep_seconds=args.sleep)
    print(f"fetched {fetched} run(s), {len(failures)} failure(s)")
    for f in failures:
        print("  ", f)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
