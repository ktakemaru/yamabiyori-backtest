"""parse.py の単体テスト。フィクスチャは 2026-09-01..02 の唐松岳に対する実レスポンス
(tests/fixtures/raw/<model>/karamatsu/2026-09.json、openmeteo_previous_runs.fetch_chunk で保存したもの)。
ネットワークには出ない。"""
from pathlib import Path

import polars as pl
import pytest

from backtest import config, parse
from backtest import openmeteo_previous_runs as om

FIXTURE_RAW = Path(__file__).parent / "fixtures" / "raw"
N_HOURS = 48  # 2日分 (UTC)


def uniq(df, col):
    return df.select(pl.col(col).unique().sort()).to_series().to_list()


@pytest.fixture(scope="module")
def envelopes():
    return {model: om.load_raw(FIXTURE_RAW / model / "karamatsu" / "2026-09.json") for model in config.MODELS}


def test_fixtures_are_real_complete_responses(envelopes):
    for model, env in envelopes.items():
        assert env is not None, model
        assert env["status"] == 200
        assert env["request"]["timezone"] == "UTC"
        assert env["request"]["wind_speed_unit"] == "ms"
        assert len(env["body"]["hourly"]["time"]) == N_HOURS


@pytest.mark.parametrize("key,expected", [
    ("cloud_cover_previous_day3", ("cloud_cover", 3)),
    ("relative_humidity_2m_previous_day7", ("relative_humidity_2m", 7)),
    ("temperature_2m", None),
    ("time", None),
])
def test_split_hourly_key(key, expected):
    assert parse.split_hourly_key(key) == expected


def test_parse_envelope_shape_and_schema(envelopes):
    df = parse.parse_envelope(envelopes["ecmwf_ifs025"])
    assert df.schema == pl.Schema(parse.SCHEMA)
    assert df.height == len(config.VARIABLES) * len(config.LEAD_DAYS) * N_HOURS
    assert set(uniq(df, "variable")) == set(config.VARIABLES)
    assert uniq(df, "lead_days") == config.LEAD_DAYS
    assert uniq(df, "site_id") == ["karamatsu"]
    assert uniq(df, "model") == ["ecmwf_ifs025"]


def test_valid_time_is_utc_and_hourly(envelopes):
    df = parse.parse_envelope(envelopes["ecmwf_ifs025"])
    sub = df.filter((pl.col("variable") == "temperature_2m") & (pl.col("lead_days") == 1))
    assert str(sub.schema["valid_time"]) == "Datetime(time_unit='us', time_zone='UTC')"
    assert sub["valid_time"][0].isoformat() == "2026-09-01T00:00:00+00:00"
    steps = sub.select(pl.col("valid_time").diff().dt.total_minutes().drop_nulls().alias("m"))
    assert steps.select((pl.col("m") == 60).all()).item()


def test_units_and_wind_in_ms(envelopes):
    df = parse.parse_envelope(envelopes["ecmwf_ifs025"])
    units = dict(df.group_by("variable").agg(pl.col("unit").first()).iter_rows())
    assert units["wind_speed_10m"] == "m/s"
    assert units["temperature_2m"] == "°C"
    assert units["cloud_cover"] == "%"
    assert units["precipitation"] == "mm"
    # 生値と一致 (加工していない)
    raw = envelopes["ecmwf_ifs025"]["body"]["hourly"]["wind_speed_10m_previous_day1"]
    got = df.filter((pl.col("variable") == "wind_speed_10m") & (pl.col("lead_days") == 1))["value"].to_list()
    assert got == pytest.approx(raw, abs=1e-6)


def test_all_null_series_are_kept_as_null(envelopes):
    """api-findings §1.1: cloud_cover_low/mid/high と wind_gusts の previous_dayN は全null。
    落とさず null 行として保持し、欠損率で見えるようにする。"""
    df = parse.parse_envelope(envelopes["ecmwf_ifs025"])
    for v in ["cloud_cover_low", "cloud_cover_mid", "cloud_cover_high", "wind_gusts_10m"]:
        sub = df.filter(pl.col("variable") == v)
        assert sub.height == len(config.LEAD_DAYS) * N_HOURS
        assert sub.select(pl.col("value").is_null().all()).item(), v
    assert not df.filter(pl.col("variable") == "cloud_cover").select(pl.col("value").is_null().any()).item()


def test_jma_msm_only_has_lead_1_and_2(envelopes):
    df = parse.parse_envelope(envelopes["jma_msm"])
    miss = (df.filter(pl.col("variable") == "temperature_2m")
              .group_by("lead_days").agg(pl.col("value").is_null().mean().alias("m")).sort("lead_days"))
    m = dict(miss.iter_rows())
    assert m[1] == 0.0 and m[2] == 0.0
    assert all(m[n] == 1.0 for n in range(3, 8))


def test_grid_metadata(envelopes):
    df = parse.parse_envelope(envelopes["jma_msm"])
    assert df["grid_lat"][0] == 36.75 and df["grid_lon"][0] == 137.75
    assert df["grid_elevation_m"][0] == 2550.0


def test_parse_raw_dir_concatenates_and_skips_broken(tmp_path, envelopes):
    import json
    import shutil
    shutil.copytree(FIXTURE_RAW, tmp_path / "raw")
    (tmp_path / "raw" / "jma_msm" / "karamatsu" / "2026-08.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "raw" / "jma_msm" / "karamatsu" / "2026-07.error.json").write_text(
        json.dumps({"body": {"error": True, "reason": "x"}}), encoding="utf-8")
    df = parse.parse_raw_dir(tmp_path / "raw")
    assert df.height == 2 * len(config.VARIABLES) * len(config.LEAD_DAYS) * N_HOURS
    assert uniq(df, "model") == sorted(config.MODELS)


def test_parquet_roundtrip(tmp_path, envelopes):
    df = parse.parse_envelope(envelopes["ecmwf_ifs025"])
    p = tmp_path / "x.parquet"
    df.write_parquet(p)
    back = pl.read_parquet(p)
    assert back.schema == df.schema
    assert back.equals(df)
