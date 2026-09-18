"""観測 (実況) を long 形式 Parquet `observation_long.parquet` にまとめる。

ソース:
1. Phase 2 で収集したアメダス正時 map (`data/obs/amedas/<JST日>.json.gz`)。source="amedas_map"。
2. 本体リポジトリの既存ハーネスが JMA etrn (過去統計, HTML) からキャッシュした毎時値
   (`C:/mountain-weather/tools/backtest/cache/amedas/<prec>_<block>/<date>.json`、読み取りのみ)。
   source="etrn_cache"。2026-01-15〜02-28, 2026-06-11〜09-14 の 6 地点。

時刻の約束: どちらも「正時 HH:00 JST に終わる 1 時間」の値 (sun1h / precipitation1h は積算、temp 等は HH:00 の瞬時値)。
etrn の hour=24 は翌日 00:00 JST。valid_time は UTC で持つ。

    python -m backtest.obs_import
    python -m backtest.obs_import --etrn-dir C:/mountain-weather/tools/backtest/cache/amedas
"""
import argparse
import gzip
import json
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import polars as pl

from . import config
from .collect_amedas import JST, OBS_DIR

log = logging.getLogger(__name__)

PARQUET_PATH = config.DATA_DIR / "parquet" / "observation_long.parquet"
DEFAULT_ETRN_DIR = Path("C:/mountain-weather/tools/backtest/cache/amedas")
# 既存ハーネス stations.py の prec_no/block_no -> アメダス地点番号 (amedastable の id)
ETRN_STATIONS = {"48_396": "48141", "48_415": "48571", "41_47690": "41166", "48_992": "48216",
                 "48_47622": "48331", "49_47640": "49251"}
ETRN_FIELDS = {"temp_c": "temp", "humidity_pct": "humidity", "sun_hr": "sun1h", "precip_mm": "precipitation1h",
               "wind_ms": "wind", "pressure_station_hpa": "pressure", "dewpoint_c": "dewpoint"}
SCHEMA = {"station_id": pl.Utf8, "source": pl.Utf8, "valid_time": pl.Datetime("us", "UTC"), "element": pl.Utf8,
          "value": pl.Float32, "quality_flag": pl.Int8}


def rows_from_amedas_maps(obs_dir: Path = OBS_DIR) -> list[dict]:
    rows = []
    for p in sorted(obs_dir.glob("*.json.gz")):
        day = json.load(gzip.open(p, "rt", encoding="utf-8"))
        d = date.fromisoformat(day["date_jst"])
        for hhmm, stations in day["hours"].items():
            t = datetime(d.year, d.month, d.day, int(hhmm[:2]), int(hhmm[2:]), tzinfo=JST).astimezone(timezone.utc)
            for sid, rec in stations.items():
                for element, v in rec.items():
                    if not isinstance(v, list) or len(v) != 2:
                        continue
                    rows.append({"station_id": sid, "source": "amedas_map", "valid_time": t.isoformat(),
                                 "element": element, "value": v[0], "quality_flag": v[1]})
    return rows


def rows_from_etrn_cache(etrn_dir: Path) -> list[dict]:
    rows = []
    if not etrn_dir.exists():
        log.warning("etrn cache dir not found: %s", etrn_dir)
        return rows
    for sub in sorted(etrn_dir.iterdir()):
        sid = ETRN_STATIONS.get(sub.name)
        if sid is None:
            log.warning("unknown etrn station dir %s (skipped)", sub.name)
            continue
        for f in sorted(sub.glob("*.json")):
            d = date.fromisoformat(f.stem)
            recs = json.load(open(f, encoding="utf-8"))
            for rec in recs:
                t = datetime(d.year, d.month, d.day, tzinfo=JST) + timedelta(hours=int(rec["hour"]))
                t = t.astimezone(timezone.utc)
                for field, element in ETRN_FIELDS.items():
                    if field in rec:
                        rows.append({"station_id": sid, "source": "etrn_cache", "valid_time": t.isoformat(),
                                     "element": element, "value": rec[field], "quality_flag": None})
    return rows


ETRN_FETCH_DIR = config.DATA_DIR / "obs" / "etrn"
ETRN_FETCH_FIELDS = ["pressure", "precipitation1h", "temp", "dewpoint", "humidity", "wind", "sun1h"]


def rows_from_etrn_fetch(etrn_fetch_dir: Path = ETRN_FETCH_DIR) -> list[dict]:
    """jma_etrn.py が取得した官署時別値 (富士山など)。source="etrn_fetch"。"""
    rows = []
    if not etrn_fetch_dir.exists():
        return rows
    for station_dir in sorted(p for p in etrn_fetch_dir.iterdir() if p.is_dir()):
        for f in sorted(station_dir.glob("*.json")):
            doc = json.loads(f.read_text(encoding="utf-8"))
            d = date.fromisoformat(doc["date_jst"])
            for rec in doc["rows"]:
                t = (datetime(d.year, d.month, d.day, tzinfo=JST) + timedelta(hours=int(rec["hour"]))).astimezone(timezone.utc)
                for element in ETRN_FETCH_FIELDS:
                    v = rec.get(element)
                    rows.append({"station_id": doc["station_id"], "source": "etrn_fetch", "valid_time": t.isoformat(),
                                 "element": element, "value": v if isinstance(v, (int, float)) else None, "quality_flag": None})
    return rows


def build(obs_dir: Path = OBS_DIR, etrn_dir: Path = DEFAULT_ETRN_DIR, etrn_fetch_dir: Path = ETRN_FETCH_DIR) -> pl.DataFrame:
    rows = rows_from_amedas_maps(obs_dir) + rows_from_etrn_cache(etrn_dir) + rows_from_etrn_fetch(etrn_fetch_dir)
    if not rows:
        return pl.DataFrame(schema=SCHEMA)
    df = pl.DataFrame(rows, schema_overrides={"value": pl.Float32, "quality_flag": pl.Int8})
    df = df.with_columns(pl.col("valid_time").str.to_datetime("%Y-%m-%dT%H:%M:%S%z", time_unit="us").dt.convert_time_zone("UTC"))
    return df.select(list(SCHEMA)).cast(SCHEMA).sort(["station_id", "element", "valid_time", "source"])


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--obs-dir", type=Path, default=OBS_DIR)
    ap.add_argument("--etrn-dir", type=Path, default=DEFAULT_ETRN_DIR)
    ap.add_argument("--out", type=Path, default=PARQUET_PATH)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    df = build(args.obs_dir, args.etrn_dir)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    df.write_parquet(args.out, compression="zstd")
    print(f"wrote {args.out}: {df.height:,} rows")
    pl.Config.set_tbl_rows(50)
    print(df.group_by("station_id", "source").agg(pl.col("valid_time").min().alias("first"), pl.col("valid_time").max().alias("last"),
                                                  pl.col("value").is_not_null().sum().alias("nonnull"), pl.len().alias("n")).sort("station_id", "source"))


if __name__ == "__main__":
    main()
