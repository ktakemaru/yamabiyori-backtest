"""再現テスト: backtest.summit_interp が本体 (C:\mountain-weather\mountain_weather_core.py) の
interp_at_altitude / add_altitude_columns と同一入力で同一出力になることを確認する。
入力は Single Runs API の実レスポンス (5地点, run 2026-09-01T00Z)。本体は import して読むだけで変更しない。"""
import gzip
import json
import sys
from pathlib import Path

import pytest

from backtest import config
from backtest import summit_interp as si

FIX = Path(__file__).parent / "fixtures"
BODY_ROOT = Path("C:/mountain-weather")

core = None
if BODY_ROOT.exists():
    sys.path.insert(0, str(BODY_ROOT))
    import mountain_weather_core as core  # noqa: E402  (読み取りのみ)

needs_body = pytest.mark.skipif(core is None, reason="本体リポジトリ C:/mountain-weather が無い")


def load_locations(model):
    with gzip.open(FIX / f"single_runs_{model}_2026-09-01T00Z.json.gz", "rt", encoding="utf-8") as f:
        return json.load(f)["body"]


@pytest.mark.parametrize("model", config.MODELS)
def test_fixture_has_pressure_levels(model):
    locs = load_locations(model)
    assert len(locs) == len(config.SITES)
    h = locs[0]["hourly"]
    for lv in [1000, 925, 850, 700, 600]:
        assert any(x is not None for x in h[f"geopotential_height_{lv}hPa"])
        assert any(x is not None for x in h[f"cloud_cover_{lv}hPa"])
    # Single Runs では 900/800hPa は両モデルとも全 null (api-findings §10)
    assert all(x is None for x in h["cloud_cover_900hPa"]) and all(x is None for x in h["cloud_cover_800hPa"])


def test_to_core_naming():
    h = {"cloud_cover_850hPa": [1], "relative_humidity_850hPa": [2], "geopotential_height_850hPa": [3], "time": ["t"]}
    out = si.to_core_naming(h)
    assert set(out) == {"cloudcover_850hPa", "relative_humidity_850hPa", "geopotential_height_850hPa", "time"}


@needs_body
@pytest.mark.parametrize("model", config.MODELS)
def test_reproduces_body_interpolation_exactly(model):
    assert si.LEVEL_STACK_HPA == core.LEVEL_STACK_HPA
    locs = load_locations(model)
    compared = 0
    for site, loc in zip(config.SITES, locs):
        hourly = si.to_core_naming(loc["hourly"])
        targets = {"summit": site["elevation_m"], **{f"{a}m": a for a in core.FIXED_ALTITUDE_BANDS_M}}
        # 本体: add_altitude_columns が hourly[altitude_col(kind, label)] を書き込む
        body_hourly = dict(hourly)
        core.add_altitude_columns(body_hourly, targets, ["cloudcover", "relative_humidity", "temperature"])
        for label, alt in targets.items():
            for kind in ["cloudcover", "relative_humidity", "temperature"]:
                ours = si.series_at_altitude(hourly, kind, alt)
                theirs = body_hourly[core.altitude_col(kind, label)]
                assert len(ours) == len(theirs) == len(hourly["time"])
                assert ours == theirs, (site["site_id"], label, kind)
                compared += sum(1 for x in theirs if x is not None)
    assert compared > 1000, "比較した非 null 値が少なすぎる"


@needs_body
def test_reproduces_body_single_hour_semantics():
    """クランプ・線形内挿・欠損面スキップの 3 ケースを本体関数と 1 時刻ずつ突き合わせる。"""
    loc = load_locations("ecmwf_ifs025")[1]   # fuji
    hourly = si.to_core_naming(loc["hourly"])
    idx = 12
    for target in [50.0, 1500.0, 3776.0, 9000.0]:
        assert si.interp_at_altitude(hourly, "cloudcover", idx, target) == core.interp_at_altitude(hourly, "cloudcover", idx, target)
        assert si.interp_at_altitude(hourly, "relative_humidity", idx, target) == core.interp_at_altitude(hourly, "relative_humidity", idx, target)
    assert si.level_profile(hourly, "cloudcover", idx) == core.level_profile(hourly, "cloudcover", idx)
    # 900/800 が null なので profile は 5 点
    assert len(si.level_profile(hourly, "cloudcover", idx)) == 5
