"""系統3 (Ensemble API 全メンバー収集 + 本体確信度スナップショット) の単体テスト。実レスポンスのフィクスチャ、ネットワーク不使用。

本体 (C:\\mountain-weather) は読み取り専用で import する。本体が無い環境ではその部分だけ skip。"""
import gzip
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import polars as pl
import pytest

from backtest import collect_ensemble as ce
from backtest import plugin_confidence_snapshot as pcs

FIX = Path(__file__).parent / "fixtures"
PLUGIN_DIR = Path(os.environ.get("YAMABIYORI_PLUGIN_DIR", "C:/mountain-weather"))


def load_fixture(name):
    with gzip.open(FIX / name, "rt", encoding="utf-8") as f:
        return json.load(f)


class FakeResponse:
    def __init__(self, body, status=200, url="https://example/"):
        self._body, self.status_code, self.url = body, status, url
        self.text = json.dumps(body)

    def json(self):
        return self._body


class FakeSession:
    """URL のパターンごとに固定レスポンスを返す。"""
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        for key, body in self.routes.items():
            if key in url:
                return FakeResponse(body, url=url)
        raise AssertionError(f"unexpected url {url}")


def test_run_from_meta_and_paths():
    meta = {"last_run_initialisation_time": 1789711200, "last_run_modification_time": 1789743571}
    run = ce.run_from_meta(meta)
    assert run == datetime(2026, 9, 18, 6, tzinfo=timezone.utc)
    assert ce.snapshot_path(run, Path("/x")) == Path("/x/2026-09-18/06Z_ecmwf_ifs025_members.json.gz")
    assert pcs.snapshot_path(run, Path("/y")) == Path("/y/2026-09-18/06Z.json.gz")
    assert ce.run_from_meta({"error": True}) is None


def test_build_params_native_3h_and_vars():
    p = ce.build_params()
    assert p["temporal_resolution"] == "native" and p["models"] == "ecmwf_ifs025" and p["timezone"] == "UTC"
    assert p["latitude"].count(",") == 4
    vs = p["hourly"].split(",")
    assert "precipitation" in vs and "cloud_cover_850hPa" in vs and "geopotential_height_600hPa" in vs
    assert not any("900hPa" in v or "800hPa" in v for v in vs)   # Ensemble API では null


def test_native_fixture_is_3_hourly_with_51_columns():
    raw = load_fixture("ensemble_ecmwf_ifs025_native_karamatsu_2026-09-18.json.gz")
    h = raw["body"]["hourly"]
    assert h["time"][1] == "2026-09-18T03:00" and len(h["time"]) == 128
    cols = [k for k in h if k.startswith("cloud_cover_member")]
    assert len(cols) == 50 and "cloud_cover" in h


def test_collect_saves_envelope_for_target_run(tmp_path, monkeypatch):
    raw = load_fixture("ensemble_ecmwf_ifs025_native_karamatsu_2026-09-18.json.gz")
    meta = {"last_run_initialisation_time": 1789711200, "last_run_modification_time": 1789743571}
    s = FakeSession({"static/meta.json": meta, "v1/ensemble": raw["body"]})
    now = datetime(2026, 9, 18, 21, tzinfo=timezone.utc)
    r = ce.collect(session=s, run_hours=[0], snapshot_dir=tmp_path, now=now)
    assert r["action"] == "skip_run_hour"
    r = ce.collect(session=s, run_hours=[6], snapshot_dir=tmp_path, now=now)
    assert r["action"] == "saved"
    env = ce.load_gz(Path(r["path"]))
    assert env["run_utc"] == "2026-09-18T06:00:00+00:00" and env["meta_before"] == meta and env["body"]["hourly"]["time"][0] == "2026-09-18T00:00"
    assert ce.collect(session=s, run_hours=[6], snapshot_dir=tmp_path, now=now)["action"] == "skip_exists"


@pytest.mark.skipif(not (PLUGIN_DIR / "mountain_weather_detail.py").exists(), reason="plugin checkout not present")
def test_plugin_confidence_reproduces_snapshot_without_touching_plugin_dir(tmp_path):
    fx = load_fixture("plugin_confidence_karamatsu_2026-09-18T06Z.json.gz")
    before_pyc = set((PLUGIN_DIR / "__pycache__").glob("*")) if (PLUGIN_DIR / "__pycache__").exists() else set()
    before_cache = set((PLUGIN_DIR / "cache").glob("ensemble_*")) if (PLUGIN_DIR / "cache").exists() else set()
    core, detail = pcs.load_plugin(PLUGIN_DIR)
    assert sys.dont_write_bytecode is True
    store = {}
    s = FakeSession({"v1/ensemble": fx["ensemble_body"]})
    detail.fetch_ensemble = pcs.make_nocache_fetch(s, detail, store)
    mtn = fx["mountain"]
    hpa = core.nearest_pressure_level(mtn["elevation_m"])
    assert hpa == fx["pressure_level_hpa"]
    out = detail.compute_ensemble_confidence_by_day(mtn["lat"], mtn["lon"], fx["cloud_var"], fx["temp_var"], fx["daily_sunrise"])
    assert store["request"]["hourly"] == fx["ensemble_request"]["hourly"] and store["request"]["timezone"] == "Asia/Tokyo"
    # 同じ入力 → 同じ出力 (cutoff は date.today() 依存なので、記録時点以降の日だけ比べる)。
    # フィクスチャは本体 a65656e (R8 前, ENSEMBLE_PRECIP_WET_THRESHOLD_MM=0.1) で記録した。閾値が変わった本体では
    # precip 系の値だけ変わるので、閾値が一致するときは全項目、違うときは閾値に依存しない項目だけを比べる。
    common = [d for d in fx["confidence_by_day"] if d in out]
    assert common, "no overlapping days (fixture too old for CONFIDENCE_MIN_DAYS_OUT cutoff?)"
    same_threshold = detail.ENSEMBLE_PRECIP_WET_THRESHOLD_MM == fx["plugin_constants"]["ENSEMBLE_PRECIP_WET_THRESHOLD_MM"]
    keys = None if same_threshold else ["cloud_confidence", "temp_confidence", "cloud_pct", "precip_mm"]
    for d in common:
        if keys is None:
            assert out[d] == fx["confidence_by_day"][d]
        else:
            assert {k: out[d][k] for k in keys} == {k: fx["confidence_by_day"][d][k] for k in keys}
    # 本体ディレクトリに何も書いていない
    after_pyc = set((PLUGIN_DIR / "__pycache__").glob("*")) if (PLUGIN_DIR / "__pycache__").exists() else set()
    after_cache = set((PLUGIN_DIR / "cache").glob("ensemble_*")) if (PLUGIN_DIR / "cache").exists() else set()
    assert after_pyc == before_pyc and after_cache == before_cache
    ident = pcs.plugin_identity(PLUGIN_DIR)
    assert ident["sha256"].get("mountain_weather_detail.py") and (ident["git_hash"] is None or len(ident["git_hash"]) == 40)


def test_rate_limit_retry_waits_then_succeeds(monkeypatch):
    class S:
        def __init__(self):
            self.n = 0

        def get(self, url, params=None, timeout=None):
            self.n += 1
            return FakeResponse({"error": True, "reason": "Minutely API request limit exceeded"}, status=429) if self.n < 3 else FakeResponse({"ok": 1})
    waits = []
    monkeypatch.setattr(ce.time, "sleep", lambda s: waits.append(s))
    r = ce.get_with_rate_limit_retry(S(), "https://x/", {}, timeout=1)
    assert r.status_code == 200 and waits == [ce.RATE_LIMIT_WAIT_SECONDS] * 2


def test_confidence_by_hash_summary_and_crosshash(tmp_path):
    from backtest import confidence_by_hash as cbh
    fx = load_fixture("plugin_confidence_karamatsu_2026-09-18T06Z.json.gz")
    site = {k: fx[k] for k in ("site_id", "mountain", "cloud_var", "temp_var", "daily_sunrise", "confidence_by_day")}
    for run, h, ver, th in [("2026-09-18T06:00:00+00:00", "a65656e" + "0" * 33, None, 0.1), ("2026-09-19T00:00:00+00:00", "7c9e8f6" + "0" * 33, "1.5.0", 0.2)]:
        env = {"run_utc": run, "fetched_at": run, "plugin": {"git_hash": h, "git_dirty_tracked": False, "plugin_version": ver},
               "plugin_constants": {"ENSEMBLE_PRECIP_WET_THRESHOLD_MM": th}, "sites": [site]}
        ce.save_gz(tmp_path / run[:10] / f"{run[11:13]}Z.json.gz", env)
    df = cbh.load_rows(tmp_path)
    s = cbh.summary(df)
    assert s.height == 2 and set(s["plugin_hash"]) == {"a65656e", "7c9e8f6"} and s.filter(pl.col("plugin_hash") == "7c9e8f6")["plugin_version"][0] == "1.5.0"
    x = cbh.crosshash(df)
    assert not x.is_empty() and set(x["plugin_hash"]) == {"a65656e", "7c9e8f6"}   # 同じ (site, day) が 2 hash で並ぶ
