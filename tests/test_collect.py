"""Phase 2 収集スクリプトの単体テスト。HTTP はモック。フィクスチャ:
- tests/fixtures/amedas_map_20260918211000.json.gz : 実際の全国10分値マップ (2026-09-18 21:10 JST)
- tests/fixtures/snapshot_06Z_jma_msm.json.gz     : 実際に保存した Forecast API スナップショット
"""
import gzip
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backtest import check_collection, config
from backtest import collect_amedas as ca
from backtest import collect_forecast_snapshot as cs

FIX = Path(__file__).parent / "fixtures"
JST = ca.JST


@pytest.fixture(scope="module")
def amedas_map():
    with gzip.open(FIX / "amedas_map_20260918211000.json.gz", "rt", encoding="utf-8") as f:
        return json.load(f)


# ---- 系統1: アメダス ----

def test_extract_keeps_only_target_stations_and_elements(amedas_map):
    out = ca.extract(amedas_map)
    assert set(out) <= set(config.AMEDAS_STATIONS)
    assert "48141" in out and "50066" in out          # 白馬, 富士山
    assert out["48141"]["temp"] == [17.6, 0]           # 実レスポンスの値そのまま [値, 品質フラグ]
    assert set(out["48141"]) == set(config.AMEDAS_ELEMENTS)
    assert set(out["50066"]) == {"temp", "sun10m", "sun1h"}   # 富士山は temp と sun のみ観測
    assert "11001" not in out                          # 対象外 (宗谷岬) は落ちる


def test_wanted_and_missing_hours(tmp_path):
    latest = datetime(2026, 9, 18, 21, 10, tzinfo=JST)
    hours = ca.wanted_hours(latest, retention_hours=5)
    assert [h.hour for h in hours] == [16, 17, 18, 19, 20, 21]
    assert ca.missing_hours(hours, tmp_path) == hours
    day = ca.load_day(ca.day_path(hours[0], tmp_path))
    day["hours"]["1700"] = {}
    ca.save_day(ca.day_path(hours[0], tmp_path), day)
    assert [h.hour for h in ca.missing_hours(hours, tmp_path)] == [16, 18, 19, 20, 21]


class Resp:
    def __init__(self, status, body=None, text=""):
        self.status_code, self._body, self.text, self.url = status, body, text, "https://example/x"

    def json(self):
        return self._body

    def raise_for_status(self):
        pass


class FakeSession:
    def __init__(self, router):
        self.router, self.calls = router, []

    def get(self, url, headers=None, timeout=None):
        self.calls.append(url)
        return self.router(url)


def test_collect_backfills_only_missing_hours_and_is_idempotent(tmp_path, monkeypatch, amedas_map):
    monkeypatch.setattr(ca.time, "sleep", lambda s: None)
    now = datetime(2026, 9, 18, 21, 10, tzinfo=JST)

    def router(url):
        if url.endswith("latest_time.txt"):
            return Resp(200, text=now.isoformat())
        if "2026091816" in url:          # 1つだけ 404 (まだ公開されていない/期限切れ)
            return Resp(404, text="not found")
        return Resp(200, body=amedas_map)

    session = FakeSession(router)
    monkeypatch.setattr(ca, "RETENTION_HOURS", 5)
    res = ca.collect(session, obs_dir=tmp_path, now=now)
    assert res["fetched"] == 5 and res["not_found"] == 1 and res["errors"] == 0
    day = ca.load_day(tmp_path / "2026-09-18.json.gz")
    assert sorted(day["hours"]) == ["1700", "1800", "1900", "2000", "2100"]
    assert day["hours"]["2100"]["48141"]["sun1h"] == [0.0, 0]
    # 2回目: 404 だった 16 時だけ再試行し、他は取りに行かない
    session2 = FakeSession(lambda url: Resp(200, body=amedas_map))
    res2 = ca.collect(session2, obs_dir=tmp_path, now=now)
    assert res2["fetched"] == 1 and [u for u in session2.calls if "map/" in u] == [ca.map_url(now.replace(hour=16, minute=0))]
    assert len(ca.load_day(tmp_path / "2026-09-18.json.gz")["hours"]) == 6


def test_collect_fetches_newest_first_within_request_cap(tmp_path, monkeypatch, amedas_map):
    monkeypatch.setattr(ca.time, "sleep", lambda s: None)
    monkeypatch.setattr(ca, "RETENTION_HOURS", 5)
    now = datetime(2026, 9, 18, 21, 10, tzinfo=JST)
    session = FakeSession(lambda url: Resp(200, body=amedas_map))
    ca.collect(session, obs_dir=tmp_path, now=now, max_requests=2)
    assert sorted(ca.load_day(tmp_path / "2026-09-18.json.gz")["hours"]) == ["2000", "2100"]


def test_day_file_crosses_jst_midnight(tmp_path):
    t = datetime(2026, 9, 18, 23, 0, tzinfo=JST)
    assert ca.day_path(t, tmp_path).name == "2026-09-18.json.gz"
    assert ca.day_path(t + timedelta(hours=1), tmp_path).name == "2026-09-19.json.gz"
    # UTC で渡しても JST 日付で保存される
    assert ca.day_path(datetime(2026, 9, 18, 15, 30, tzinfo=timezone.utc), tmp_path).name == "2026-09-19.json.gz"


# ---- 系統2: Forecast スナップショット ----

@pytest.mark.parametrize("now,expected", [
    ("2026-09-18T13:10:00+00:00", "2026-09-18T06:00:00+00:00"),   # 実際に取った回
    ("2026-09-18T03:20:00+00:00", "2026-09-18T00:00:00+00:00"),
    ("2026-09-18T02:59:00+00:00", "2026-09-17T18:00:00+00:00"),   # 3h 猶予前は前スロット
    ("2026-09-18T09:20:00+00:00", "2026-09-18T06:00:00+00:00"),
    ("2026-09-18T09:55:00+00:00", "2026-09-18T06:00:00+00:00"),   # cron が 35 分遅れても同じスロット
])
def test_slot_for(now, expected):
    assert cs.slot_for(datetime.fromisoformat(now)).isoformat() == expected


def test_build_params_covers_all_sites_and_layered_cloud():
    p = cs.build_params("ecmwf_ifs025")
    assert len(p["latitude"].split(",")) == len(config.SITES)
    for v in ["cloud_cover_low", "cloud_cover_mid", "cloud_cover_high"]:
        assert v in p["hourly"].split(",")
    assert p["timezone"] == "UTC" and p["wind_speed_unit"] == "ms" and p["forecast_days"] == 16


def test_saved_snapshot_fixture_has_metadata_and_layered_cloud():
    env = cs.load_snapshot(FIX / "snapshot_06Z_jma_msm.json.gz")
    assert env["model"] == "jma_msm" and env["slot_utc"] == "2026-09-18T06:00:00+00:00"
    assert env["fetched_at"].endswith("+00:00")
    assert len(env["body"]) == len(env["sites"])
    h = env["body"][0]["hourly"]
    assert any(x is not None for x in h["cloud_cover_low"])


def test_take_snapshot_skips_existing_and_saves_new(tmp_path, monkeypatch):
    env = cs.load_snapshot(FIX / "snapshot_06Z_jma_msm.json.gz")
    slot = datetime.fromisoformat(env["slot_utc"])

    class S:
        calls = 0

        def get(self, url, params=None, timeout=None):
            S.calls += 1
            return Resp(200, body=env["body"])

    s = S()
    p = cs.take_snapshot(s, slot, "jma_msm", snapshot_dir=tmp_path)
    assert p == tmp_path / "2026-09-18" / "06Z_jma_msm.json.gz" and p.exists()
    saved = cs.load_snapshot(p)
    assert saved["body"] == env["body"] and saved["request"]["models"] == "jma_msm"
    assert cs.take_snapshot(s, slot, "jma_msm", snapshot_dir=tmp_path) is None
    assert S.calls == 1


# ---- チェックスクリプト ----

def test_check_collection_reports_gaps(tmp_path):
    now = datetime(2026, 9, 18, 13, 10, tzinfo=timezone.utc)   # 22:10 JST
    obs, snap = tmp_path / "obs", tmp_path / "snap"
    # アメダス: 9/17 は全24時間、9/18 は 21 時が抜けている
    for d, hours in [("2026-09-17", range(24)), ("2026-09-18", [h for h in range(22) if h != 21])]:
        p = obs / f"{d}.json.gz"
        day = ca.load_day(p)
        for h in hours:
            day["hours"][f"{h:02d}00"] = {sid: {"temp": [1.0, 0]} for sid in config.AMEDAS_STATIONS}
        ca.save_day(p, day)
    lines, problems = check_collection.check_amedas(1, now, obs)
    assert any("2026-09-18" in p and "2100" in p for p in problems)
    assert not any("2026-09-17" in p for p in problems)
    # スナップショット: 最新スロット (06Z) の ecmwf だけ無い
    for slot_h, model in [("00", "jma_msm"), ("00", "ecmwf_ifs025"), ("06", "jma_msm")]:
        p = snap / "2026-09-18" / f"{slot_h}Z_{model}.json.gz"
        p.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(p, "wt") as f:
            f.write("{}")
    lines, problems = check_collection.check_snapshots(0, now, snap)
    assert problems == ["snapshot missing 2026-09-18 06Z ecmwf_ifs025"]
