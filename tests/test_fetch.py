"""openmeteo_previous_runs.py の単体テスト。HTTP はモックし、ネットワークには出ない。"""
import json
from datetime import date
from pathlib import Path

import pytest

from backtest import config
from backtest import openmeteo_previous_runs as om

FIXTURE_RAW = Path(__file__).parent / "fixtures" / "raw"


def test_month_chunks_splits_on_calendar_months():
    chunks = om.month_chunks(date(2025, 9, 18), date(2026, 1, 5))
    assert chunks[0] == (date(2025, 9, 18), date(2025, 9, 30))
    assert chunks[1] == (date(2025, 10, 1), date(2025, 10, 31))
    assert chunks[-1] == (date(2026, 1, 1), date(2026, 1, 5))
    assert len(chunks) == 5
    assert om.month_chunks(date(2026, 2, 3), date(2026, 2, 3)) == [(date(2026, 2, 3), date(2026, 2, 3))]
    assert om.month_chunks(date(2026, 3, 1), date(2026, 2, 1)) == []


def test_default_period_is_364_days_ending_yesterday():
    start, end = config.default_period(date(2026, 9, 18))
    assert end == date(2026, 9, 17)
    assert (end - start).days == 364


def test_hourly_param_covers_all_variables_and_leads():
    p = config.hourly_param().split(",")
    assert len(p) == len(config.VARIABLES) * len(config.LEAD_DAYS)
    assert "cloud_cover_previous_day1" in p and "relative_humidity_2m_previous_day7" in p


def test_jma_msm_requests_only_lead_1_and_2():
    assert config.leads_for("jma_msm") == [1, 2] and config.leads_for("ecmwf_ifs025") == list(range(1, 8))
    params = om.build_params(config.SITES[0], "jma_msm", date(2026, 9, 1), date(2026, 9, 30))
    cols = params["hourly"].split(",")
    assert len(cols) == len(config.VARIABLES) * 2
    assert all(c.endswith(("_previous_day1", "_previous_day2")) for c in cols)


def test_raw_path_layout():
    p = om.raw_path("jma_msm", "karamatsu", date(2026, 9, 1), raw_dir=Path("/x"))
    assert p == Path("/x/jma_msm/karamatsu/2026-09.json.gz")


def test_is_complete_true_for_fixture_false_for_broken(tmp_path):
    assert om.is_complete(FIXTURE_RAW / "ecmwf_ifs025" / "karamatsu" / "2026-09.json")
    assert not om.is_complete(tmp_path / "missing.json")
    p = tmp_path / "broken.json"
    p.write_text("{", encoding="utf-8")
    assert not om.is_complete(p)
    p.write_text(json.dumps({"body": {"error": True, "reason": "bad"}}), encoding="utf-8")
    assert not om.is_complete(p)
    p.write_text(json.dumps({"body": {"hourly": {"time": []}}}), encoding="utf-8")
    assert not om.is_complete(p)


class FakeResponse:
    def __init__(self, status, body, url="https://example/x"):
        self.status_code, self._body, self.url, self.text = status, body, url, json.dumps(body)

    def json(self):
        return self._body


class FakeSession:
    """呼ばれた回数と params を記録し、順に用意した応答を返す。"""
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def get(self, url, params=None, timeout=None):
        self.calls.append(params)
        return self.responses.pop(0)


def fixture_body():
    return om.load_raw(FIXTURE_RAW / "ecmwf_ifs025" / "karamatsu" / "2026-09.json")["body"]


def test_fetch_chunk_saves_envelope_then_skips_on_rerun(tmp_path, monkeypatch):
    monkeypatch.setattr(om.time, "sleep", lambda s: None)
    site = config.SITES[0]
    session = FakeSession([FakeResponse(200, fixture_body())])
    p = om.fetch_chunk(session, site, "ecmwf_ifs025", date(2026, 9, 1), date(2026, 9, 2), raw_dir=tmp_path)
    assert p.exists() and om.is_complete(p)
    env = om.load_raw(p)
    assert p.name.endswith(".json.gz")
    assert env["request"]["latitude"] == site["lat"]
    assert env["request"]["timezone"] == "UTC" and env["request"]["wind_speed_unit"] == "ms"
    assert env["request"]["start_date"] == "2026-09-01" and env["request"]["end_date"] == "2026-09-02"
    assert env["fetched_at"].endswith("+00:00")
    assert len(session.calls) == 1
    # 2回目はネットワークに出ない
    session2 = FakeSession([])
    p2 = om.fetch_chunk(session2, site, "ecmwf_ifs025", date(2026, 9, 1), date(2026, 9, 2), raw_dir=tmp_path)
    assert p2 == p and session2.calls == []


def test_fetch_chunk_api_error_is_saved_as_error_file_and_raised(tmp_path, monkeypatch):
    monkeypatch.setattr(om.time, "sleep", lambda s: None)
    session = FakeSession([FakeResponse(400, {"error": True, "reason": "Invalid value"})])
    with pytest.raises(RuntimeError, match="Invalid value"):
        om.fetch_chunk(session, config.SITES[0], "jma_msm", date(2026, 9, 1), date(2026, 9, 30), raw_dir=tmp_path)
    assert (tmp_path / "jma_msm" / "karamatsu" / "2026-09.error.json.gz").exists()
    assert not om.is_complete(tmp_path / "jma_msm" / "karamatsu" / "2026-09.json.gz")


def test_request_with_retry_backs_off_on_429_then_succeeds(monkeypatch):
    sleeps = []
    monkeypatch.setattr(om.time, "sleep", lambda s: sleeps.append(s))
    session = FakeSession([FakeResponse(429, {"error": True, "reason": "rate"}),
                           FakeResponse(503, {"error": True, "reason": "down"}),
                           FakeResponse(200, fixture_body())])
    status, body, _ = om.request_with_retry(session, {"x": 1})
    assert status == 200 and "hourly" in body
    assert len(session.calls) == 3 and sleeps == [5.0, 10.0]


def test_fetch_all_only_sleeps_for_network_calls(tmp_path, monkeypatch):
    sleeps = []
    monkeypatch.setattr(om.time, "sleep", lambda s: sleeps.append(s))
    session = FakeSession([FakeResponse(200, fixture_body()) for _ in range(4)])
    sites = config.SITES[:2]
    paths, failures = om.fetch_all(sites, ["ecmwf_ifs025"], date(2026, 8, 20), date(2026, 9, 2), raw_dir=tmp_path,
                                   sleep_seconds=0.1, session=session)
    assert len(session.calls) == 4 and len(paths) == 4 and failures == []  # 2 sites x 2 months
    assert sleeps == [0.1] * 4
    # 再実行: 全てスキップ、sleep 無し
    session2 = FakeSession([])
    om.fetch_all(sites, ["ecmwf_ifs025"], date(2026, 8, 20), date(2026, 9, 2), raw_dir=tmp_path,
                 sleep_seconds=0.1, session=session2)
    assert session2.calls == [] and sleeps == [0.1] * 4


def test_fetch_all_continues_after_a_failed_chunk(tmp_path, monkeypatch):
    monkeypatch.setattr(om.time, "sleep", lambda s: None)
    session = FakeSession([FakeResponse(400, {"error": True, "reason": "boom"}),
                           FakeResponse(200, fixture_body())])
    paths, failures = om.fetch_all(config.SITES[:1], ["ecmwf_ifs025"], date(2026, 8, 1), date(2026, 9, 2),
                                   raw_dir=tmp_path, sleep_seconds=0, session=session)
    assert len(paths) == 1 and len(failures) == 1 and "2026-08" in failures[0]
    assert om.is_complete(tmp_path / "ecmwf_ifs025" / "karamatsu" / "2026-09.json.gz")


def test_request_with_retry_retries_non_json_200(monkeypatch):
    """実際に観測した 'HTTP 200 + 非JSON本文 (streaming timeoutReached)' は一時エラーとして再試行する。"""
    monkeypatch.setattr(om.time, "sleep", lambda s: None)

    class NonJson(FakeResponse):
        def __init__(self):
            self.status_code, self.url = 200, "https://example/x"
            self.text = "Unexpected error while streaming data: timeoutReached"

        def json(self):
            raise ValueError("no json")

    session = FakeSession([NonJson(), FakeResponse(200, fixture_body())])
    status, body, _ = om.request_with_retry(session, {})
    assert status == 200 and "hourly" in body and len(session.calls) == 2
