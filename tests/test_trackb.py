"""Track B のパース・観測取り込み・評価関数の単体テスト (実レスポンスのフィクスチャ、ネットワーク不使用)。"""
import gzip
import json
import math
from datetime import datetime, timezone
from pathlib import Path

import polars as pl
import pytest

from backtest import config, obs_import, parse_single_runs as psr, trackb_eval as te
from backtest import openmeteo_single_runs as osr

FIX = Path(__file__).parent / "fixtures"


def envelope(model):
    with gzip.open(FIX / f"single_runs_{model}_2026-09-01T00Z.json.gz", "rt", encoding="utf-8") as f:
        raw = json.load(f)
    # probe 時の生レスポンス (url/status/body) を fetch_run と同じ envelope 形式に包む
    return {"model": model, "run_utc": "2026-09-01T00:00:00+00:00", "sites": [s["site_id"] for s in config.SITES[:5]],
            "site_defs": config.SITES[:5], "request": {}, "url": raw["url"], "status": raw["status"],
            "fetched_at": "2026-09-18T12:00:00+00:00", "body": raw["body"]}


def test_hourly_vars_exclude_900_800():
    vs = osr.hourly_vars()
    assert "relative_humidity_850hPa" in vs and "geopotential_height_600hPa" in vs
    assert not any("900hPa" in v or "800hPa" in v for v in vs)


def test_runs_between_and_first_run_clamp():
    from datetime import date
    runs = osr.runs_between(date(2026, 9, 1), date(2026, 9, 2), hours=[0, 12])
    assert [r.isoformat() for r in runs] == ["2026-09-01T00:00:00+00:00", "2026-09-01T12:00:00+00:00",
                                             "2026-09-02T00:00:00+00:00", "2026-09-02T12:00:00+00:00"]
    assert osr.raw_path("jma_msm", runs[1], Path("/x")) == Path("/x/jma_msm/2026-09-01T12Z.json.gz")


@pytest.mark.parametrize("model", config.MODELS)
def test_parse_envelope_long_shape(model):
    df = psr.parse_envelope(envelope(model))
    assert df.schema == pl.Schema(psr.SCHEMA)
    assert set(df["site_id"].unique()) == {s["site_id"] for s in config.SITES[:5]}
    lv = df.filter(pl.col("variable") == "relative_humidity")["level_hpa"].unique().sort().to_list()
    assert lv == [600, 700, 800, 850, 900, 925, 1000]
    derived = df.filter(pl.col("variable") == "cloud_cover_at_summit")
    assert derived["level_hpa"].null_count() == derived.height and derived.height > 0
    # lead_hours = valid - run
    first = df.filter(pl.col("variable") == "temperature_2m").sort("valid_time").row(0, named=True)
    assert first["lead_hours"] == 0 and first["run_utc"] == datetime(2026, 9, 1, tzinfo=timezone.utc)


def test_parse_derived_matches_direct_interp():
    env = envelope("ecmwf_ifs025")
    df = psr.parse_envelope(env)
    from backtest import summit_interp as si
    loc = env["body"][1]   # fuji
    h = si.to_core_naming(loc["hourly"])
    direct = si.series_at_altitude(h, "relative_humidity", 3776, levels=osr.LEVELS_HPA)
    got = df.filter((pl.col("site_id") == "fuji") & (pl.col("variable") == "relative_humidity_at_summit")).sort("valid_time")["value"].to_list()
    assert len(got) == len(direct)
    assert all((a is None and b is None) or abs(a - b) < 1e-4 for a, b in zip(got, direct))


def test_obs_import_amedas_and_etrn(tmp_path):
    # アメダス日次ファイル (フィクスチャの map から 1 時刻分作る)
    from backtest import collect_amedas as ca
    with gzip.open(FIX / "amedas_map_20260918211000.json.gz", "rt", encoding="utf-8") as f:
        mp = json.load(f)
    obs_dir = tmp_path / "obs"
    day = ca.load_day(obs_dir / "2026-09-18.json.gz")
    day["hours"]["2100"] = ca.extract(mp)
    ca.save_day(obs_dir / "2026-09-18.json.gz", day)
    # etrn キャッシュ風ファイル (hour=24 は翌日 00:00 JST)
    etrn = tmp_path / "etrn" / "48_396"
    etrn.mkdir(parents=True)
    (etrn / "2026-06-11.json").write_text(json.dumps([{"hour": 1, "temp_c": 10.0, "sun_hr": 0.0, "humidity_pct": 90},
                                                       {"hour": 24, "temp_c": 12.5, "sun_hr": 0.7, "humidity_pct": None}]), encoding="utf-8")
    df = obs_import.build(obs_dir, tmp_path / "etrn")
    a = df.filter((pl.col("source") == "amedas_map") & (pl.col("station_id") == "48141") & (pl.col("element") == "temp"))
    assert a["value"][0] == pytest.approx(17.6) and a["valid_time"][0] == datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
    e = df.filter((pl.col("source") == "etrn_cache") & (pl.col("element") == "sun1h")).sort("valid_time")
    assert e["station_id"][0] == "48141"
    assert e["valid_time"].to_list() == [datetime(2026, 6, 10, 16, 0, tzinfo=timezone.utc), datetime(2026, 6, 11, 15, 0, tzinfo=timezone.utc)]
    assert e["value"].to_list() == pytest.approx([0.0, 0.7])


def test_contingency_math():
    d = pl.DataFrame({"model": ["m"] * 4, "cloud": [10.0, 80.0, 10.0, 80.0], "obs_sunny": [True, False, False, True]})
    c = te.contingency(d, "cloud", ["model"]).row(0, named=True)
    assert (c["hit"], c["false_alarm"], c["miss"], c["correct_neg"]) == (1, 1, 1, 1)
    assert c["accuracy"] == 0.5 and c["false_alarm_ratio"] == 0.5 and c["miss_rate"] == 0.5 and c["baseline_acc"] == 0.5


def test_logp_interpolation_weight():
    # 650hPa は 700 と 600 の ln(p) で約 0.48 の位置
    w = (math.log(650) - math.log(700)) / (math.log(600) - math.log(700))
    assert 0.47 < w < 0.49


def test_auc_and_pss_basic():
    from backtest import skill
    a, se, n1, n0 = skill.auc([0.9, 0.8, 0.3, 0.2], [True, True, False, False])
    assert a == 1.0 and n1 == 2 and n0 == 2
    a, *_ = skill.auc([0.5, 0.5, 0.5, 0.5], [True, True, False, False])
    assert a == 0.5
    p, pod, pofd, *_ = skill.pss([True, True, True, True], [True, False, True, False])
    assert p == 0.0 and pod == 1.0 and pofd == 1.0
    best, th = skill.pss_max([0.9, 0.8, 0.3, 0.2], [True, True, False, False])
    assert best == 1.0 and th == 0.8


def test_delong_paired_test_identical_and_different():
    from backtest import skill
    lab = [True, False] * 20
    s = [0.6 + 0.01 * i if l else 0.4 - 0.01 * i for i, l in enumerate(lab)]
    r = skill.delong_paired_test(s, s, lab)
    assert r["diff"] == 0 and r["p"] == 1.0
    noise = [x + (0.5 if i % 3 == 0 else -0.5) for i, x in enumerate(s)]   # 一方を劣化させる
    r = skill.delong_paired_test(s, noise, lab)
    assert r["auc_a"] > r["auc_b"] and r["diff"] > 0 and 0 <= r["p"] <= 1 and r["se"] > 0


def test_quantile_map_primitives():
    from backtest import quantile_map as qm
    # PAV: 増加している所だけ併合される (重み付き平均)
    assert qm.pav_decreasing([0.9, 0.5, 0.7, 0.1], [1, 1, 1, 1]) == [0.9, 0.6, 0.6, 0.1]
    assert qm.pav_decreasing([0.9, 0.2, 0.8, 0.1], [1, 3, 1, 1]) == pytest.approx([0.9, 0.35, 0.35, 0.1])
    # 分位 (mid-rank) とビン
    cdf = [1.0, 2.0, 2.0, 4.0]
    assert qm.quantile_of(cdf, 2.0) == pytest.approx(0.5) and qm.quantile_of(cdf, 0.5) == 0.0 and qm.quantile_of(cdf, 9.0) == 1.0
    assert qm.qbin_of(0.999) == 9 and qm.qbin_of(1.0) == 9 and qm.qbin_of(0.0) == 0
    assert [qm.rawbin_of(v) for v in (0, 0.5, 10, 10.1, 100)] == [0, 1, 1, 2, 10]


def test_quantile_map_fit_apply_and_brier():
    from backtest import quantile_map as qm
    # 2 地点: 0% は晴れ 3/4、正値は雲量が増えるほど曇り
    rows = []
    for pair in ("a", "b"):
        rows += [(pair, 0.0, True)] * 30 + [(pair, 0.0, False)] * 10
        for v in range(1, 101):
            rows.append((pair, float(v), v <= 40))
    train = pl.DataFrame(rows, schema=["pair", "value_h1", "obs_sunny"], orient="row")
    cdfs = qm.site_cdfs(train)
    m = qm.fit_qmap(train, cdfs)
    assert m["p_zero"] == pytest.approx(0.75) and m["n_zero"] == 80
    assert m["p_bin"] == sorted(m["p_bin"], reverse=True) and m["p_bin"][0] == 1.0 and m["p_bin"][-1] == 0.0
    assert qm.apply_qmap(m, cdfs["a"], 0.0) == 0.75 and qm.apply_qmap(m, cdfs["a"], 5.0) == 1.0 and qm.apply_qmap(m, cdfs["a"], 95.0) == 0.0
    # Brier 分解: 完全予報は 0、定数予報は reliability=(p−ȳ)², resolution=0
    p = [1.0, 0.0, 1.0, 0.0]; y = [True, False, True, False]
    d = qm.brier_decomposition(p, y)
    assert d["brier"] == 0.0 and d["resolution"] == pytest.approx(0.25) and d["uncertainty"] == 0.25
    d = qm.brier_decomposition([0.7] * 4, y)
    assert d["brier"] == pytest.approx(0.29) and d["reliability"] == pytest.approx(0.04) and d["resolution"] == 0.0
