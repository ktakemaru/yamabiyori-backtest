"""生レスポンス (openmeteo_previous_runs.py が保存した envelope) → long 形式 DataFrame。
design.md §2 のスキーマ。ネットワークには一切出ない。"""
import re
from pathlib import Path

import polars as pl

from . import openmeteo_previous_runs as om

PREVIOUS_DAY_RE = re.compile(r"^(?P<variable>.+)_previous_day(?P<lead>\d+)$")

SCHEMA = {
    "site_id": pl.Utf8, "model": pl.Utf8, "variable": pl.Utf8, "lead_days": pl.Int8,
    "valid_time": pl.Datetime("us", "UTC"), "value": pl.Float32, "unit": pl.Utf8,
    "grid_lat": pl.Float64, "grid_lon": pl.Float64, "grid_elevation_m": pl.Float32,
    "fetched_at": pl.Datetime("us", "UTC"),
}


def split_hourly_key(key: str) -> tuple[str, int] | None:
    """'cloud_cover_previous_day3' -> ('cloud_cover', 3)。previous_day 無しなら None。"""
    m = PREVIOUS_DAY_RE.match(key)
    if not m:
        return None
    return m.group("variable"), int(m.group("lead"))


def parse_envelope(env: dict) -> pl.DataFrame:
    """1つの生ファイルを long 形式に展開する。時刻は request の timezone が UTC である前提
    (config.REQUEST_UNITS)。"""
    body = env["body"]
    hourly = body["hourly"]
    units = body.get("hourly_units", {})
    assert env["request"].get("timezone") == "UTC", "valid_time は UTC 前提。timezone が違う生ファイル"
    # 時刻は生JSONの ISO 文字列のまま列にし、最後に str.to_datetime() でまとめて変換する
    times = list(hourly["time"])
    fetched_at = env["fetched_at"]
    frames = []
    for key, values in hourly.items():
        if key == "time":
            continue
        parsed = split_hourly_key(key)
        if parsed is None:
            continue
        variable, lead = parsed
        if len(values) != len(times):
            raise ValueError(f"{key}: {len(values)} values for {len(times)} timestamps")
        n = len(times)
        # 全 null の系列 (cloud_cover_low 等) は型推論できないので dtype を明示して Series を作る
        frames.append(pl.DataFrame([
            pl.Series("site_id", [env["site"]["site_id"]] * n, dtype=pl.Utf8),
            pl.Series("model", [env["model"]] * n, dtype=pl.Utf8),
            pl.Series("variable", [variable] * n, dtype=pl.Utf8),
            pl.Series("lead_days", [lead] * n, dtype=pl.Int8),
            pl.Series("valid_time", times, dtype=pl.Utf8),
            pl.Series("value", values, dtype=pl.Float32),
            pl.Series("unit", [units.get(key)] * n, dtype=pl.Utf8),
            pl.Series("grid_lat", [body.get("latitude")] * n, dtype=pl.Float64),
            pl.Series("grid_lon", [body.get("longitude")] * n, dtype=pl.Float64),
            pl.Series("grid_elevation_m", [body.get("elevation")] * n, dtype=pl.Float32),
            pl.Series("fetched_at", [fetched_at] * n, dtype=pl.Utf8),
        ]))
    if not frames:
        return pl.DataFrame(schema=SCHEMA)
    df = pl.concat(frames)
    df = df.with_columns(
        pl.col("valid_time").str.to_datetime("%Y-%m-%dT%H:%M", time_unit="us").dt.replace_time_zone("UTC"),
        pl.col("fetched_at").str.to_datetime("%Y-%m-%dT%H:%M:%S%z", time_unit="us").dt.convert_time_zone("UTC"),
    )
    return df.select(list(SCHEMA)).cast(SCHEMA)


def parse_raw_dir(raw_dir: Path) -> pl.DataFrame:
    """raw_dir 配下の完全な生ファイルを全て読み、1つの long DataFrame にする。壊れたファイルは無視して警告。"""
    import logging
    log = logging.getLogger(__name__)
    frames = []
    for path in sorted(list(raw_dir.rglob("*.json")) + list(raw_dir.rglob("*.json.gz"))):
        if ".error.json" in path.name:
            continue
        env = om.load_raw(path)
        if env is None:
            log.warning("skipping unreadable/incomplete raw file: %s", path)
            continue
        frames.append(parse_envelope(env))
    if not frames:
        return pl.DataFrame(schema=SCHEMA)
    return pl.concat(frames).sort(["model", "site_id", "variable", "lead_days", "valid_time"])
