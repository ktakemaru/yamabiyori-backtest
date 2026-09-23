"""等価性テスト: mos + 新形式の表 (mos_tables/r1-summit-cloud-sunny.json) が、本体 v1.5.0 の R1 補正と同一入力で同一出力になる。

本体は固定コミット BODY_COMMIT の mountain_weather_core.py を `git show` で一時フォルダに取り出して import する
(本体の作業ツリーは R1 の無いブランチを checkout していることがあるため、作業ツリーは読まない)。
git は --no-optional-locks で読み取り系コマンド (cat-file / rev-parse / show) だけを使い、本体の .git にも作業ツリーにも書かない。
本体リポジトリ・git・当該コミットのいずれかが無い環境ではスキップ。ネットワークには出ない。

本体の lead は「取得日を 1 とする暦日差」(core.lead_day_for) で、mos は「ラン初期時刻からの経過時間」を受け取る。
v1.5.0 と同じ行を引かせるため、下の body_glue は本体の lead_day d を 24·(d−1) 時間として渡す (R11 参照)。
本体は夜間・暖候期外にも適用するので on_mismatch="allow" で呼ぶ。
"""
import gzip
import importlib.util
import json
import math
import os
import shutil
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

import mos
from backtest import config
from backtest import summit_interp as si

REPO = Path(__file__).resolve().parent.parent
FIX = Path(__file__).parent / "fixtures"
TABLE_PATH = REPO / "mos_tables" / "r1-summit-cloud-sunny.json"
BODY_ROOT = Path(os.environ.get("YAMABIYORI_PLUGIN_DIR", "C:/mountain-weather"))
BODY_COMMIT = "52d3d2a5560ec35329de1b47f6a836b658db15d6"           # main = v1.5.0 (R1 は 53e4e3b 以降このコミットまで core 無変更)
BODY_CORE_BLOB = "1aaf5d83668ffbd77bc91640fc9a15960696c868"        # 52d3d2a:mountain_weather_core.py
KW = {"predictor": "cloud_cover_at_summit", "target": "p_sunny"}


def _git(*args):
    return subprocess.run(["git", "--no-optional-locks", "-C", str(BODY_ROOT), *args], capture_output=True, timeout=60)


@pytest.fixture(scope="module")
def core(tmp_path_factory):
    if not BODY_ROOT.exists() or shutil.which("git") is None:
        pytest.skip("本体リポジトリまたは git が無い")
    if _git("cat-file", "-e", f"{BODY_COMMIT}^{{commit}}").returncode != 0:
        pytest.skip(f"本体にコミット {BODY_COMMIT[:7]} が無い")
    blob = _git("rev-parse", f"{BODY_COMMIT}:mountain_weather_core.py").stdout.decode().strip()
    assert blob == BODY_CORE_BLOB, "固定コミットの mountain_weather_core.py が想定と違う"
    src = _git("show", f"{BODY_COMMIT}:mountain_weather_core.py")
    assert src.returncode == 0
    path = tmp_path_factory.mktemp("body_v150") / "mountain_weather_core_v150.py"
    path.write_bytes(src.stdout)
    name = "mountain_weather_core_v150"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    old = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.dont_write_bytecode = old
    yield mod
    sys.modules.pop(name, None)


@pytest.fixture(scope="module")
def ts():
    return mos.parse_table_set(json.loads(TABLE_PATH.read_text(encoding="utf-8")))


def body_glue(ts, core, hourly: dict, summit_m: float, today: date = None) -> list:
    """本体 calibrated_summit_cloud_series の置き換え例: 本体の糊 (モデル選択・lead の数え方・標高ランプの混合) + mos.calibrate。
    標高ランプ (w) は本体に残る。v1.5.0 の設定では全山頂 w=1 だが、混合式 round(w*cal + (1-w)*raw, 1) は残す
    (raw が NaN だと 0.0*NaN で NaN になるのは本体の挙動。mos 単体は NaN を最終ビンとして扱う = core.calibrated_cloud_pct と同じ)。"""
    w = core.cloud_calibration_weight(summit_m)
    assert w == 1.0
    raw = hourly[core.SUMMIT_VARS["cloudcover"]]
    times = hourly["time"]
    today = today or date.fromisoformat(times[0][:10])
    msm = hourly.get(core.MSM_PRECIP_VAR) or [None] * len(times)
    out = []
    for i, t in enumerate(times):
        model = "jma_msm" if (i < len(msm) and msm[i] is not None) else "ecmwf_ifs025"
        lead_day = max(1, (date.fromisoformat(t[:10]) - today).days + 1)
        r = mos.calibrate(ts, raw[i], model=model, lead_hours=24 * (lead_day - 1), hour_jst=int(t[11:13]), month=int(t[5:7]),
                          on_mismatch="allow", **KW)
        out.append(None if r.value is None else round(w * r.value + (1.0 - w) * raw[i], 1))
    return out


# ---------------------------------------------------------------- 表そのもの
def test_table_equals_body_constants(core, ts):
    assert list(ts.edges) == [float(e) for e in core.CLOUD_CALIBRATION_BIN_EDGES]
    assert len(ts.tables) == len(core.CLOUD_CALIBRATION_TABLE)
    for t, (model, lo, hi, bins) in zip(ts.tables, core.CLOUD_CALIBRATION_TABLE):
        assert t.model == model
        assert t.lead_hours == (24.0 * (lo - 1), 24.0 * hi)
        assert list(t.p) == list(bins)
    for t in ts.tables:
        assert ts.reference_p[t.table_id] == core.CLOUD_CALIBRATION_P_REF[t.model]
    assert core.CLOUD_CALIBRATION_ENABLED is True and core.CLOUD_CALIBRATION_FULL_SUMMIT_M is None


# ---------------------------------------------------------------- 1 値
VALUES = ([None, -5.0, 0, 0.0, 1e-9, 0.1, 9.99, 10, 10.0001, 20, 30, 40, 50, 50.0001, 70, 70.0001, 99.9, 100, 150, float("nan")]
          + [x / 2 for x in range(0, 201)])


@pytest.mark.parametrize("model", ["ecmwf_ifs025", "jma_msm", "gfs_seamless"])
def test_single_value_equivalence(core, ts, model):
    compared = 0
    for lead_day in range(1, 17):
        for v in VALUES:
            theirs = core.calibrated_cloud_pct(v, model, lead_day)
            for hour, month in [(12, 7), (22, 11)]:            # 適用範囲の内と外 (外は allow = v1.5.0 の挙動)
                ours = mos.calibrate(ts, v, model=model, lead_hours=24 * (lead_day - 1), hour_jst=hour, month=month,
                                     on_mismatch="allow", **KW).value
                assert ours == theirs and type(ours) is type(theirs), (model, lead_day, v, hour, month, ours, theirs)
                compared += 1
    assert compared == 16 * len(VALUES) * 2


def test_lead_hours_within_the_day_pick_the_same_row(core, ts):
    """本体の lead_day d に対し [24(d−1), 24d) のどの時間を渡しても同じ行 (= 同じ値)。"""
    for model in ["ecmwf_ifs025", "jma_msm"]:
        for lead_day in range(1, 13):
            theirs = core.calibrated_cloud_pct(5.0, model, lead_day)
            for h in range(24 * (lead_day - 1), 24 * lead_day):
                assert mos.calibrate(ts, 5.0, model=model, lead_hours=h, hour_jst=12, month=7, **KW).value == theirs


# ---------------------------------------------------------------- 系列 (本体の糊 + mos)
def fixture_hourly(model: str, site: dict, msm_hours: int) -> dict:
    with gzip.open(FIX / f"single_runs_{model}_2026-09-01T00Z.json.gz", "rt", encoding="utf-8") as f:
        loc = json.load(f)["body"][[s["site_id"] for s in config.SITES].index(site["site_id"])]
    hourly = si.to_core_naming(loc["hourly"])
    n = len(hourly["time"])
    return {"time": hourly["time"],
            "cloudcover_at_summit": si.series_at_altitude(hourly, "cloudcover", site["elevation_m"]),
            "precipitation_msm": [0.0] * min(msm_hours, n) + [None] * max(0, n - msm_hours)}


@pytest.mark.parametrize("model, msm_hours, min_compared", [("ecmwf_ifs025", 0, 1000), ("ecmwf_ifs025", 78, 1000), ("jma_msm", 96, 300)])
def test_series_equivalence_on_fixtures(core, ts, model, msm_hours, min_compared):
    compared = 0
    for site in config.SITES:
        hourly = fixture_hourly(model, site, msm_hours)
        assert core.SUMMIT_VARS["cloudcover"] == "cloudcover_at_summit" and core.MSM_PRECIP_VAR == "precipitation_msm"
        theirs = core.calibrated_summit_cloud_series(hourly, site["elevation_m"])
        ours = body_glue(ts, core, hourly, site["elevation_m"])
        assert ours == theirs, site["site_id"]
        compared += sum(1 for x in theirs if x is not None)
    assert compared > min_compared


def test_series_equivalence_synthetic(core, ts):
    """日付・月をまたぎ、MSM の有無と None が混ざる系列。today を過去に置く (scratch_validate_refs と同じ呼び方) 場合も。"""
    start = datetime(2026, 9, 28, 0, 0)
    times = [(start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M") for i in range(24 * 12)]
    raw = [None if i % 37 == 5 else VALUES[1 + i % (len(VALUES) - 2)] for i in range(len(times))]
    msm = [0.0 if i < 51 else None for i in range(len(times))]
    hourly = {"time": times, "cloudcover_at_summit": raw, "precipitation_msm": msm}
    for summit_m in (1700.0, 3180.0, 3776.0):
        for today in (None, date(2026, 9, 26), date(2026, 10, 2)):
            theirs = core.calibrated_summit_cloud_series(hourly, summit_m, today=today)
            ours = body_glue(ts, core, hourly, summit_m, today=today)
            assert len(ours) == len(theirs)
            for a, b in zip(ours, theirs):
                assert a == b or (a is not None and b is not None and math.isnan(a) and math.isnan(b)), (a, b)


# ---------------------------------------------------------------- R12 候補の表 (MSM d1-2 行だけ 7 面で学習し直したもの)
NEW_TABLE_PATH = REPO / "mos_tables" / "r1-summit-cloud-sunny-msm7.json"
TP_TABLE_PATH = REPO / "mos_tables" / "r1-summit-cloud-sunny-msm7-transplant.json"


@pytest.fixture(scope="module", params=[NEW_TABLE_PATH, TP_TABLE_PATH], ids=["msm7", "msm7-transplant"])
def ts_new(request):
    """R12 の候補表 2 つ (Historical 7 面で学習し直した表 / 旧表 + 面の数の効果の移植表)。どちらも d1-2 以外は v1.5.0 と同じはず。"""
    return mos.parse_table_set(json.loads(request.param.read_text(encoding="utf-8")))


def test_new_table_unchanged_rows_equal_v150(core, ts_new):
    """ECMWF の全行と MSM の d3-4 行 (lead_day 3 以降、範囲外は nearest) は、旧基準値のまま = 本体 v1.5.0 と完全一致。未知モデルも ECMWF と同じ。"""
    compared = 0
    for model, lead_days in [("ecmwf_ifs025", range(1, 17)), ("gfs_seamless", range(1, 17)), ("jma_msm", range(3, 17))]:
        for lead_day in lead_days:
            for v in VALUES:
                theirs = core.calibrated_cloud_pct(v, model, lead_day)
                ours = mos.calibrate(ts_new, v, model=model, lead_hours=24 * (lead_day - 1), hour_jst=22, month=11,
                                     on_mismatch="allow", **KW).value
                assert ours == theirs and type(ours) is type(theirs), (model, lead_day, v, ours, theirs)
                compared += 1
    assert compared == (16 + 16 + 14) * len(VALUES)
    assert ts_new.reference_p["jma_msm/h48-96"] == core.CLOUD_CALIBRATION_P_REF["jma_msm"]


def test_new_table_msm_d12_row_is_retrained(core, ts_new):
    t = next(x for x in ts_new.tables if x.table_id == "jma_msm/h0-48")
    p_ref = ts_new.reference_p[t.table_id]
    assert p_ref == t.p[0] and p_ref != core.CLOUD_CALIBRATION_P_REF["jma_msm"]
    assert t.meta["training_source"]["n_levels"] == 7 and 900 in t.meta["training_source"]["levels_hpa"]
    for lead_day in (1, 2):
        r = mos.calibrate(ts_new, 0.0, model="jma_msm", lead_hours=24 * (lead_day - 1), hour_jst=12, month=7, **KW)
        assert r.value == 0.0 and r.table_id == "jma_msm/h0-48"                       # 案C: d1-2 の 0% は実効 0%
        for v in VALUES[1:]:
            if v != v:          # NaN
                continue
            p = t.p[mos.bin_index(ts_new.edges, v)]
            ours = mos.calibrate(ts_new, v, model="jma_msm", lead_hours=24 * (lead_day - 1), hour_jst=12, month=7, **KW).value
            assert ours == round(min(100.0, max(0.0, 100.0 * (1.0 - p / p_ref))), 1)


def test_transplant_row_is_old_row_times_ratio(ts):
    """移植表の MSM d1-2 行 = 旧表の行 × (Historical 7 面 / 5 面) の比 (PAV 後、3 桁に丸め)。記録した比と p_old から再計算して一致。"""
    doc = json.loads(TP_TABLE_PATH.read_text(encoding="utf-8"))
    row = next(t for t in doc["tables"] if t["table_id"] == "jma_msm/h0-48")
    old_row = next(t for t in ts.tables if t.table_id == "jma_msm/h0-48")
    assert [b["p_old"] for b in row["bins"]] == list(old_row.p)
    tp = row["transplant"]
    assert tp["method"] == "ratio" and len(tp["ratio_h7_h5"]) == len(row["bins"])
    for b, a5, a7 in zip(row["bins"], tp["p_h5"], tp["p_h7"]):
        assert b["p"] == round(b["p_old"] * a7 / a5, 3)
    assert row["normalization"]["p_ref"] == row["bins"][0]["p"]
    assert "移植" in row["training_source"]["note"] and "移植" in row["status"]


# ---------------------------------------------------------------- 本体 v1.6.0 (R12: MSM d1-2 行を 7 面で学び直し、P_ref を行ごとに)
BODY_COMMIT_V160 = "43557eacece2c4b4b70587e52401955808596cbf"      # feature/r12-msm7 (案 A、本体 r12-plugin-integration-plan.md)
BODY_CORE_BLOB_V160 = "51b38a60da7fa3a5ee93aa272c9e9524207a50a8"   # 43557ea:mountain_weather_core.py


@pytest.fixture(scope="module")
def core160(tmp_path_factory):
    if not BODY_ROOT.exists() or shutil.which("git") is None:
        pytest.skip("本体リポジトリまたは git が無い")
    if _git("cat-file", "-e", f"{BODY_COMMIT_V160}^{{commit}}").returncode != 0:
        pytest.skip(f"本体にコミット {BODY_COMMIT_V160[:7]} が無い")
    blob = _git("rev-parse", f"{BODY_COMMIT_V160}:mountain_weather_core.py").stdout.decode().strip()
    assert blob == BODY_CORE_BLOB_V160
    src = _git("show", f"{BODY_COMMIT_V160}:mountain_weather_core.py")
    path = tmp_path_factory.mktemp("body_v160") / "mountain_weather_core_v160.py"
    path.write_bytes(src.stdout)
    name = "mountain_weather_core_v160"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    old = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.dont_write_bytecode = old
    yield mod
    sys.modules.pop(name, None)


@pytest.fixture(scope="module")
def ts_msm7():
    return mos.parse_table_set(json.loads(NEW_TABLE_PATH.read_text(encoding="utf-8")))


@pytest.mark.parametrize("model", ["ecmwf_ifs025", "jma_msm", "gfs_seamless"])
def test_v160_single_value(core, core160, ts_msm7, model):
    """MSM の lead_day 1-2 は新表 (mos) と一致、それ以外はすべて v1.5.0 と一致 (型も)。"""
    changed = same = 0
    for lead_day in range(1, 17):
        for v in VALUES:
            ours = core160.calibrated_cloud_pct(v, model, lead_day)
            if model == "jma_msm" and lead_day <= 2:
                expect = mos.calibrate(ts_msm7, v, model=model, lead_hours=24 * (lead_day - 1), hour_jst=12, month=7, **KW).value
                changed += 1
            else:
                expect = core.calibrated_cloud_pct(v, model, lead_day)
                same += 1
            assert ours == expect and type(ours) is type(expect), (model, lead_day, v, ours, expect)
            # 新表 (mos) とは全範囲で一致
            m = mos.calibrate(ts_msm7, v, model=model, lead_hours=24 * (lead_day - 1), hour_jst=22, month=11, on_mismatch="allow", **KW).value
            assert ours == m and type(ours) is type(m)
    assert same + changed == 16 * len(VALUES)


def test_v160_table_rows(core, core160):
    """表: MSM d1-2 行だけが新表の値、他の行と各行の P_ref は v1.5.0 の値。"""
    old_rows = {(r[0], r[1]): r for r in core.CLOUD_CALIBRATION_TABLE}
    for r in core160.CLOUD_CALIBRATION_TABLE:
        o = old_rows[(r[0], r[1])]
        assert r[:3] == o[:3]
        if (r[0], r[1]) == ("jma_msm", 1):
            assert r[3] == [0.822, 0.687, 0.517, 0.342, 0.260, 0.142, 0.062, 0.041] and r[4] == 0.822
        else:
            assert r[3] == o[3] and r[4] == core.CLOUD_CALIBRATION_P_REF[r[0]]
    assert not hasattr(core160, "CLOUD_CALIBRATION_P_REF")


@pytest.mark.parametrize("model, msm_hours, min_compared", [("ecmwf_ifs025", 0, 1000), ("ecmwf_ifs025", 78, 1000), ("jma_msm", 96, 300)])
def test_v160_series_on_fixtures(core, core160, ts_msm7, model, msm_hours, min_compared):
    compared = 0
    for site in config.SITES:
        hourly = fixture_hourly(model, site, msm_hours)
        new = core160.calibrated_summit_cloud_series(hourly, site["elevation_m"])
        assert new == body_glue(ts_msm7, core160, hourly, site["elevation_m"])
        old = core.calibrated_summit_cloud_series(hourly, site["elevation_m"])
        today = date.fromisoformat(hourly["time"][0][:10])
        for i, t in enumerate(hourly["time"]):
            lead_day = max(1, (date.fromisoformat(t[:10]) - today).days + 1)
            is_msm = hourly["precipitation_msm"][i] is not None
            if not (is_msm and lead_day <= 2):
                assert new[i] == old[i] or (new[i] != new[i] and old[i] != old[i]), (site["site_id"], t)
        compared += sum(1 for x in new if x is not None)
    assert compared > min_compared


def test_v160_series_synthetic(core, core160, ts_msm7):
    start = datetime(2026, 9, 28, 0, 0)
    times = [(start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M") for i in range(24 * 12)]
    raw = [None if i % 37 == 5 else VALUES[1 + i % (len(VALUES) - 2)] for i in range(len(times))]
    msm = [0.0 if i < 90 else None for i in range(len(times))]
    hourly = {"time": times, "cloudcover_at_summit": raw, "precipitation_msm": msm}
    for summit_m in (1700.0, 3180.0, 3776.0):
        for today in (None, date(2026, 9, 26), date(2026, 10, 2)):
            new = core160.calibrated_summit_cloud_series(hourly, summit_m, today=today)
            glued = body_glue(ts_msm7, core160, hourly, summit_m, today=today)
            old = core.calibrated_summit_cloud_series(hourly, summit_m, today=today)
            t0 = today or date.fromisoformat(times[0][:10])
            for i, t in enumerate(times):
                a, b, c = new[i], glued[i], old[i]
                assert a == b or (a is not None and b is not None and math.isnan(a) and math.isnan(b))
                lead_day = max(1, (date.fromisoformat(t[:10]) - t0).days + 1)
                if not (msm[i] is not None and lead_day <= 2):
                    assert a == c or (a is not None and c is not None and math.isnan(a) and math.isnan(c))
