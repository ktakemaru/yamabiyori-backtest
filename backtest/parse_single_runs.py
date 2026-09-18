"""Single Runs の生レスポンス (openmeteo_single_runs.py) → long 形式 Parquet `single_runs_long.parquet`。

行 = (site, model, run, variable, level_hpa, valid_time)。気圧面変数は level_hpa 付き、地上変数は level_hpa null。
さらに本体と同じ内挿 (summit_interp) で、各地点の山頂標高とペア観測所標高における
cloud_cover / relative_humidity を派生行として追加する (variable 例: `cloud_cover_at_summit`, `relative_humidity_at_station`)。
lead_hours = valid_time − run。Track B 評価 (trackb_eval.py) はこの Parquet だけを読む。

    python -m backtest.parse_single_runs
"""
import argparse
import logging
import re
from datetime import datetime
from pathlib import Path

import polars as pl

from . import config
from . import openmeteo_single_runs as osr
from . import summit_interp as si

log = logging.getLogger(__name__)

PARQUET_PATH = config.DATA_DIR / "parquet" / "single_runs_long.parquet"
LEVEL_RE = re.compile(r"^(?P<var>.+)_(?P<hpa>\d+)hPa$")
SCHEMA = {
    "site_id": pl.Utf8, "model": pl.Utf8, "run_utc": pl.Datetime("us", "UTC"), "variable": pl.Utf8,
    "level_hpa": pl.Int16, "valid_time": pl.Datetime("us", "UTC"), "lead_hours": pl.Int16,
    "value": pl.Float32, "grid_elevation_m": pl.Float32,
}

# 派生列の目標高度: 山頂 (本体が使う値) と、ペア観測所の標高 (実況と同じ高さで ②③ を測るため)
STATION_ELEVATION_M = {"48141": 703, "50066": 3775, "48571": 1350, "41166": 1292, "36196": 1220}


def _frame(site_id, model, run_iso, variable, level, times, values, elev):
    n = len(times)
    return pl.DataFrame([
        pl.Series("site_id", [site_id] * n, dtype=pl.Utf8), pl.Series("model", [model] * n, dtype=pl.Utf8),
        pl.Series("run_utc", [run_iso] * n, dtype=pl.Utf8), pl.Series("variable", [variable] * n, dtype=pl.Utf8),
        pl.Series("level_hpa", [level] * n, dtype=pl.Int16), pl.Series("valid_time", times, dtype=pl.Utf8),
        pl.Series("value", values, dtype=pl.Float32), pl.Series("grid_elevation_m", [elev] * n, dtype=pl.Float32),
    ])


def parse_envelope(env: dict) -> pl.DataFrame:
    frames = []
    run_iso = env["run_utc"]
    for site, loc in zip(env["site_defs"], env["body"]):
        hourly = loc["hourly"]
        times = list(hourly["time"])
        elev = loc.get("elevation")
        for key, values in hourly.items():
            if key == "time":
                continue
            m = LEVEL_RE.match(key)
            var, level = (m.group("var"), int(m.group("hpa"))) if m else (key, None)
            frames.append(_frame(site["site_id"], env["model"], run_iso, var, level, times, values, elev))
        # 本体と同じ内挿 (Single Runs で取れる 5 面のみ、api-findings §10)
        core_named = si.to_core_naming(hourly)
        targets = {"summit": site["elevation_m"], "station": STATION_ELEVATION_M.get(site.get("station_id"))}
        for label, alt in targets.items():
            if alt is None:
                continue
            for kind, out_var in [("cloudcover", "cloud_cover"), ("relative_humidity", "relative_humidity")]:
                vals = si.series_at_altitude(core_named, kind, alt, levels=osr.LEVELS_HPA)
                frames.append(_frame(site["site_id"], env["model"], run_iso, f"{out_var}_at_{label}", None, times, vals, elev))
    df = pl.concat(frames)
    df = df.with_columns(
        pl.col("valid_time").str.to_datetime("%Y-%m-%dT%H:%M", time_unit="us").dt.replace_time_zone("UTC"),
        pl.col("run_utc").str.to_datetime("%Y-%m-%dT%H:%M:%S%z", time_unit="us").dt.convert_time_zone("UTC"),
    ).with_columns(
        ((pl.col("valid_time") - pl.col("run_utc")).dt.total_hours()).cast(pl.Int16).alias("lead_hours"),
    )
    return df.select(list(SCHEMA)).cast(SCHEMA)


def parse_raw_dir(raw_dir: Path = osr.RAW_DIR) -> pl.DataFrame:
    frames = []
    for path in sorted(raw_dir.rglob("*.json.gz")):
        env = osr.load_raw(path)
        if env is None:
            log.warning("skipping unreadable raw file: %s", path)
            continue
        frames.append(parse_envelope(env))
    if not frames:
        return pl.DataFrame(schema=SCHEMA)
    return pl.concat(frames).sort(["model", "site_id", "run_utc", "variable", "level_hpa", "valid_time"])


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--raw-dir", type=Path, default=osr.RAW_DIR)
    ap.add_argument("--out", type=Path, default=PARQUET_PATH)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    df = parse_raw_dir(args.raw_dir)
    # 欠損 (null) 行は落とす: ホライズン外の詰め物と 900/800hPa
    df = df.filter(pl.col("value").is_not_null())
    args.out.parent.mkdir(parents=True, exist_ok=True)
    df.write_parquet(args.out, compression="zstd")
    print(f"wrote {args.out}: {df.height:,} rows")
    print(df.group_by("model").agg(pl.col("run_utc").n_unique().alias("runs"), pl.col("run_utc").min().alias("first_run"),
                                   pl.col("run_utc").max().alias("last_run"), pl.col("lead_hours").max().alias("max_lead_h")).sort("model"))
    print(df.group_by("model", "variable").agg(pl.len().alias("n")).sort("model", "variable"))


if __name__ == "__main__":
    main()
