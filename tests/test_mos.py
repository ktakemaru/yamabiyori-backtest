"""mos (移植可能な補正モジュール) の単体テスト。ネットワークに出ない。本体は使わない (等価性は test_mos_equivalence.py)。"""
import ast
import copy
import json
import sys
from pathlib import Path

import pytest

import mos
from mos import ContextMismatchError, SchemaError, TableNotFoundError

REPO = Path(__file__).resolve().parent.parent
TABLE_PATH = REPO / "mos_tables" / "r1-summit-cloud-sunny.json"
LEGACY_PATH = REPO / "docs" / "cloud-calibration-table.json"
KW = {"predictor": "cloud_cover_at_summit", "target": "p_sunny"}


def load_doc():
    return json.loads(TABLE_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def ts():
    return mos.parse_table_set(load_doc())


def resealed(doc):
    doc["content_sha256"] = mos.content_sha256(doc)
    return doc


def cal(ts, v, model="ecmwf_ifs025", lead_hours=12, hour_jst=12, month=7, on_mismatch="error"):
    return mos.calibrate(ts, v, model=model, lead_hours=lead_hours, hour_jst=hour_jst, month=month, on_mismatch=on_mismatch, **KW)


# ---------------------------------------------------------------- 移植性
def test_mos_is_stdlib_only_and_py38_syntax():
    files = sorted((REPO / "mos").glob("*.py"))
    assert files
    for f in files:
        src = f.read_text(encoding="utf-8")
        tree = ast.parse(src, feature_version=(3, 8))        # match 文・X | Y の実行時評価などの新構文を避ける
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level > 0:
                    continue                                  # mos 内の相対 import
                names = [node.module.split(".")[0]]
            else:
                continue
            for n in names:
                assert n in sys.stdlib_module_names or n == "__future__", (f.name, n)


def test_version_strings():
    assert mos.__version__ == mos.MOS_VERSION
    assert len(mos.tables.version_tuple(mos.MOS_VERSION)) == 3


# ---------------------------------------------------------------- 表ファイルの中身
def test_table_file_parses_and_carries_required_metadata(ts):
    assert ts.table_set_id == "r1-summit-cloud-sunny" and ts.schema_version == 1
    assert [t.table_id for t in ts.tables] == ["ecmwf_ifs025/h0-48", "ecmwf_ifs025/h48-120", "ecmwf_ifs025/h120-240",
                                               "jma_msm/h0-48", "jma_msm/h48-96"]
    for t in ts.tables:
        m = t.meta
        assert m["time_of_day"]["label"] == "day" and t.hours_jst == tuple(range(7, 18))
        assert m["season"]["label"] == "warm" and t.months == (6, 7, 8, 9)
        assert m["observation"]["kind"] == "foot_amedas_sun1h"
        assert m["provenance"]["generator"].startswith("backtest/cloud_calibration_table.py")
        assert m["provenance"]["legacy_json"]["identical"] is True
        assert m["n"] == sum(b["n"] for b in m["bins"])
        ve = m["validated_elevation"]
        status = {s["site_id"]: s["status"] for s in ve["sites"]}
        assert status == {"adatara": "provisional", "akadake": "confirmed", "karamatsu": "confirmed", "nikko_shirane": "confirmed"}
        assert ve["confirmed_m"] == [2578, 2899] and ve["including_provisional_m"] == [1700, 2899]
        assert sum(s["n"] for s in ve["sites"]) == m["n"]


def test_table_file_equals_legacy_json():
    """新形式の p / n が既存の docs/cloud-calibration-table.json (本体 v1.5.0 に写した元) と一致する。"""
    new = load_doc()
    old = json.loads(LEGACY_PATH.read_text(encoding="utf-8"))
    assert new["binning"]["edges"] == old["bin_edges"]
    flat_old = [(m, g) for m, gs in old["table"].items() for g in gs]
    assert len(flat_old) == len(new["tables"])
    for (m, g), t in zip(flat_old, new["tables"]):
        assert t["model"] == m and t["lead_days_legacy"] == g["lead_days"]
        assert t["lead_hours"] == [24 * (g["lead_days"][0] - 1), 24 * g["lead_days"][1]]
        assert [b["p"] for b in t["bins"]] == [b["p_sunny_used"] for b in g["bins"]]
        assert [b["p_raw"] for b in t["bins"]] == [b["p_sunny"] for b in g["bins"]]
        assert [b["n"] for b in t["bins"]] == [b["n"] for b in g["bins"]]


def test_exporter_reproduces_table_file():
    """同じ parquet から mos_export.build_doc を回すと、provenance 以外は表ファイルと同じになる。"""
    pl = pytest.importorskip("polars")
    from backtest import mos_export as mx
    if not (mx.SR_PATH.exists() and mx.OBS_PATH.exists()):
        pytest.skip("parquet が無い")
    legacy = json.loads(LEGACY_PATH.read_text(encoding="utf-8"))
    git = {"commit": "x", "dirty": False, "legacy_json_commit": "y"}
    from datetime import date
    doc = mx.build_doc(pl.read_parquet(mx.SR_PATH), pl.read_parquet(mx.OBS_PATH), git, date(2000, 1, 1), legacy)

    def strip(d):
        d = copy.deepcopy(d)
        for k in ("provenance", "content_sha256"):
            d.pop(k, None)
        for t in d["tables"]:
            t.pop("provenance")
        return d
    assert strip(json.loads(json.dumps(doc, ensure_ascii=False))) == strip(load_doc())


# ---------------------------------------------------------------- 版・スキーマの検査
@pytest.mark.parametrize("mutate, msg", [
    (lambda d: d.update(schema="other"), "schema must be"),
    (lambda d: d.update(schema_version=2), "not supported"),
    (lambda d: d.update(min_mos_version="99.0.0"), "needs mos"),
    (lambda d: d.update(min_mos_version="latest"), "X.Y.Z"),
    (lambda d: d["tables"][0].pop("observation"), "missing key 'observation'"),
    (lambda d: d["tables"][0]["bins"].pop(), "bins must have"),
    (lambda d: d["tables"][0]["bins"][3].update(p=1.2), "outside"),
    (lambda d: d["tables"][0]["time_of_day"].update(label="evening"), "time_of_day.label"),
    (lambda d: d["tables"][0]["season"].update(months=[13]), "season.months"),
    (lambda d: d["tables"][1].update(lead_hours=[24, 120]), "overlap"),
    (lambda d: d["tables"][1].update(table_id="ecmwf_ifs025/h0-48"), "unique"),
    (lambda d: d["tables"][0].update(lead_hours=[0, 47]), "no reference table"),
    (lambda d: d["fallback"].update(unknown_model="gfs"), "has no table"),
    (lambda d: d["binning"].update(edges=[0, 10, 10, 30, 40, 50, 70, 100]), "strictly increasing"),
])
def test_schema_errors(mutate, msg):
    doc = load_doc()
    mutate(doc)
    with pytest.raises(SchemaError, match=msg):
        mos.parse_table_set(resealed(doc))


def test_content_hash_detects_hand_edit():
    doc = load_doc()
    doc["tables"][0]["bins"][1]["p"] = 0.5          # sha を更新しない手編集
    with pytest.raises(SchemaError, match="content_sha256"):
        mos.parse_table_set(doc)


# ---------------------------------------------------------------- 値
@pytest.mark.parametrize("v, b", [(-5, 0), (0, 0), (0.0, 0), (1e-9, 1), (10, 1), (10.0001, 2), (50, 5), (50.1, 6),
                                  (70, 6), (70.0001, 7), (100, 7), (150, 7)])
def test_bin_boundaries(ts, v, b):
    assert mos.bin_index(ts.edges, v) == b


def test_values_case_c(ts):
    assert cal(ts, 0.0).value == 0.0                                   # d1-2 の 0% → 実効 0%
    assert cal(ts, 0.0, lead_hours=150).value == round(100 * (1 - 0.505 / 0.737), 1) == 31.5
    assert cal(ts, 80.0).value == 100.0                                 # p = 0 → 100 (clamp 内)
    assert cal(ts, None).value is None
    t = ts.tables[0]
    assert mos.probability(ts, t, 5.0) == 0.492


def test_probability_transform_returns_p():
    doc = load_doc()
    doc["transform"] = {"kind": "probability"}
    ts = mos.parse_table_set(resealed(doc))
    assert cal(ts, 5.0).value == 0.492


# ---------------------------------------------------------------- 表の選び方
def test_lead_selection_and_fallback(ts):
    assert cal(ts, 5.0, lead_hours=0).table_id == "ecmwf_ifs025/h0-48"
    assert cal(ts, 5.0, lead_hours=47.9).table_id == "ecmwf_ifs025/h0-48"
    assert cal(ts, 5.0, lead_hours=48).table_id == "ecmwf_ifs025/h48-120"
    r = cal(ts, 5.0, lead_hours=300)
    assert r.table_id == "ecmwf_ifs025/h120-240" and "lead_nearest" in r.reason
    assert cal(ts, 5.0, lead_hours=-3).table_id == "ecmwf_ifs025/h0-48"
    assert cal(ts, 5.0, model="jma_msm", lead_hours=100).table_id == "jma_msm/h48-96"
    r = cal(ts, 5.0, model="gfs_seamless")
    assert r.table_id == "ecmwf_ifs025/h0-48" and r.reason.startswith("model_fallback:gfs_seamless->ecmwf_ifs025")


def test_lead_out_of_range_error():
    doc = load_doc()
    doc["fallback"]["lead_out_of_range"] = "error"
    ts = mos.parse_table_set(resealed(doc))
    with pytest.raises(TableNotFoundError):
        cal(ts, 5.0, lead_hours=300)


def test_unknown_model_without_fallback():
    doc = load_doc()
    doc["fallback"]["unknown_model"] = None
    ts = mos.parse_table_set(resealed(doc))
    with pytest.raises(TableNotFoundError):
        cal(ts, 5.0, model="gfs_seamless")


def test_predictor_mismatch_is_always_an_error(ts):
    for how in mos.ON_MISMATCH_CHOICES:
        with pytest.raises(TableNotFoundError):
            mos.calibrate(ts, 5.0, predictor="cloud_cover", target="p_sunny", model="ecmwf_ifs025", lead_hours=12,
                          hour_jst=22, month=7, on_mismatch=how)


# ---------------------------------------------------------------- 時刻・季節の適用範囲 (on_mismatch)
@pytest.mark.parametrize("hour, month", [(22, 7), (6, 7), (18, 7), (12, 10), (12, 5), (2, 1)])
def test_out_of_scope_error_by_default(ts, hour, month):
    with pytest.raises(ContextMismatchError):
        cal(ts, 5.0, hour_jst=hour, month=month)


def test_out_of_scope_passthrough_and_allow(ts):
    r = cal(ts, 5.0, hour_jst=22, on_mismatch="passthrough")
    assert (r.value, r.applied, r.in_scope, r.table_id) == (5.0, False, False, None) and "hour_jst=22" in r.reason
    r = cal(ts, 5.0, hour_jst=22, on_mismatch="allow")
    assert r.applied and not r.in_scope and r.value == cal(ts, 5.0).value and "out_of_scope" in r.reason
    r = cal(ts, 5.0, month=11, on_mismatch="allow")
    assert not r.in_scope and "month=11" in r.reason


def test_in_scope_edges(ts):
    for hour in (7, 17):
        for month in (6, 9):
            r = cal(ts, 5.0, hour_jst=hour, month=month)
            assert r.applied and r.in_scope and r.reason == ""


@pytest.mark.parametrize("kw", [{"on_mismatch": "ignore"}, {"hour_jst": 24}, {"hour_jst": 7.0}, {"month": 0}, {"month": True}])
def test_bad_arguments(ts, kw):
    with pytest.raises(ValueError):
        cal(ts, 5.0, **kw)
