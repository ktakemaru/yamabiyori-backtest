"""較正表 JSON (schema "yamabiyori-mos-table") の検証と読み込み。I/O はしない: 呼び出し側が json.load した dict を渡す。"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

from .errors import SchemaError

MOS_VERSION = "0.2.0"
SCHEMA_NAME = "yamabiyori-mos-table"
SUPPORTED_SCHEMA_VERSIONS = (1, 2)
BINNING_KINDS = ("zero_atom_right_closed",)
TRANSFORM_KINDS = ("probability", "ratio_to_reference")
LEAD_OUT_OF_RANGE = ("nearest_group", "error")
TIME_OF_DAY_LABELS = ("day", "night", "all")
SEASON_LABELS = ("warm", "cold", "all")
REQUIRED_TABLE_KEYS = ("table_id", "predictor", "target", "model", "lead_hours", "lead_definition", "time_of_day", "season",
                       "observation", "training_period", "validated_elevation", "provenance", "n", "bins")
# schema 2 (mos 0.2.0) で各テーブルに必須: 学習データ源と面 (training_source)、正規化の基準値 (normalization)
REQUIRED_TABLE_KEYS_V2 = ("training_source", "normalization")


@dataclass(frozen=True)
class Transform:
    kind: str
    scale: float = 100.0
    clamp: Tuple[float, float] = (0.0, 100.0)
    round_decimals: int = 1
    reference_lead_hours: Optional[Tuple[float, float]] = None
    reference_bin: int = 0


@dataclass(frozen=True)
class Table:
    table_id: str
    predictor: str
    target: str
    model: str
    lead_hours: Tuple[float, float]      # [lo, hi)
    hours_jst: Tuple[int, ...]           # 適用範囲: valid_time の JST の時 (その時刻に終わる 1 時間)
    months: Tuple[int, ...]              # 適用範囲: valid_time の JST の月
    p: Tuple[float, ...]                 # ビンごとの確率 (計算に使う値)
    meta: dict = field(repr=False, compare=False)


@dataclass(frozen=True)
class TableSet:
    table_set_id: str
    table_set_version: str
    schema_version: int
    edges: Tuple[float, ...]
    transform: Transform
    unknown_model: Optional[str]
    lead_out_of_range: str
    tables: Tuple[Table, ...]
    reference_p: Dict[str, float] = field(repr=False, compare=False)   # table_id -> p_ref (ratio_to_reference のとき)
    doc: dict = field(repr=False, compare=False)


def content_sha256(doc: dict) -> str:
    """content_sha256 キーを除いた文書の正規化 JSON の SHA-256。書き出し側と読み込み側で同じ関数を使う。"""
    body = {k: v for k, v in doc.items() if k != "content_sha256"}
    s = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def version_tuple(v: str) -> Tuple[int, ...]:
    try:
        return tuple(int(x) for x in v.split("."))
    except (AttributeError, ValueError):
        raise SchemaError("version must look like 'X.Y.Z': %r" % (v,))


def _req(obj: dict, key: str, where: str):
    if not isinstance(obj, dict) or key not in obj:
        raise SchemaError("%s: missing key %r" % (where, key))
    return obj[key]


def _number(x, where: str) -> float:
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        raise SchemaError("%s: not a number: %r" % (where, x))
    return float(x)


def _int_list(xs, lo: int, hi: int, where: str) -> Tuple[int, ...]:
    if not isinstance(xs, list) or not xs:
        raise SchemaError("%s: must be a non-empty list" % where)
    for x in xs:
        if isinstance(x, bool) or not isinstance(x, int) or not lo <= x <= hi:
            raise SchemaError("%s: %r is not an integer in [%d, %d]" % (where, x, lo, hi))
    return tuple(xs)


def _parse_transform(tr: dict, schema_version: int) -> Transform:
    kind = _req(tr, "kind", "transform")
    if kind not in TRANSFORM_KINDS:
        raise SchemaError("transform.kind %r not in %r" % (kind, TRANSFORM_KINDS))
    if kind == "probability":
        return Transform(kind=kind)
    clamp = _req(tr, "clamp", "transform")
    rd = _req(tr, "round_decimals", "transform")
    if isinstance(rd, bool) or not isinstance(rd, int):
        raise SchemaError("transform.round_decimals must be an integer")
    if not isinstance(clamp, list) or len(clamp) != 2:
        raise SchemaError("transform.clamp must be [lo, hi]")
    common = dict(kind=kind, scale=_number(_req(tr, "scale", "transform"), "transform.scale"),
                  clamp=(_number(clamp[0], "clamp"), _number(clamp[1], "clamp")), round_decimals=rd)
    if schema_version >= 2 and "reference" not in tr:
        return Transform(**common)          # schema 2: 基準値は各テーブルの normalization.p_ref
    ref = _req(tr, "reference", "transform")
    lead = _req(ref, "lead_hours", "transform.reference")
    if not isinstance(lead, list) or len(lead) != 2:
        raise SchemaError("transform.reference.lead_hours must be [lo, hi]")
    return Transform(reference_lead_hours=(_number(lead[0], "reference"), _number(lead[1], "reference")),
                     reference_bin=_req(ref, "bin", "transform.reference"), **common)


def _parse_table(t: dict, n_bins: int, schema_version: int = 1) -> Table:
    for k in REQUIRED_TABLE_KEYS + (REQUIRED_TABLE_KEYS_V2 if schema_version >= 2 else ()):
        _req(t, k, "table %s" % t.get("table_id", "?") if isinstance(t, dict) else "table")
    where = "table %s" % t["table_id"]
    lead = t["lead_hours"]
    if not isinstance(lead, list) or len(lead) != 2:
        raise SchemaError("%s: lead_hours must be [lo, hi)" % where)
    lo, hi = _number(lead[0], where), _number(lead[1], where)
    if not lo < hi:
        raise SchemaError("%s: lead_hours lo must be < hi" % where)
    tod, season = t["time_of_day"], t["season"]
    if _req(tod, "label", where) not in TIME_OF_DAY_LABELS:
        raise SchemaError("%s: time_of_day.label must be one of %r" % (where, TIME_OF_DAY_LABELS))
    if _req(season, "label", where) not in SEASON_LABELS:
        raise SchemaError("%s: season.label must be one of %r" % (where, SEASON_LABELS))
    hours = _int_list(_req(tod, "hours_jst", where), 0, 23, where + " time_of_day.hours_jst")
    months = _int_list(_req(season, "months", where), 1, 12, where + " season.months")
    bins = t["bins"]
    if not isinstance(bins, list) or len(bins) != n_bins:
        raise SchemaError("%s: bins must have %d entries (binning.edges)" % (where, n_bins))
    p = []
    for b in bins:
        v = _number(_req(b, "p", where), where + " bins.p")
        if not 0.0 <= v <= 1.0:
            raise SchemaError("%s: p %r outside [0, 1]" % (where, v))
        p.append(v)
    return Table(table_id=t["table_id"], predictor=t["predictor"], target=t["target"], model=t["model"],
                 lead_hours=(lo, hi), hours_jst=hours, months=months, p=tuple(p), meta=t)


def parse_table_set(doc: dict) -> TableSet:
    """json.load 済みの表を検証して TableSet にする。合わなければ SchemaError (黙って読み進めない)。"""
    if not isinstance(doc, dict):
        raise SchemaError("table document must be a JSON object")
    if doc.get("schema") != SCHEMA_NAME:
        raise SchemaError("schema must be %r, got %r" % (SCHEMA_NAME, doc.get("schema")))
    sv = doc.get("schema_version")
    if sv not in SUPPORTED_SCHEMA_VERSIONS:
        raise SchemaError("schema_version %r not supported by mos %s (supported: %r)" % (sv, MOS_VERSION, SUPPORTED_SCHEMA_VERSIONS))
    need = _req(doc, "min_mos_version", "document")
    if version_tuple(need) > version_tuple(MOS_VERSION):
        raise SchemaError("table needs mos >= %s, this is mos %s" % (need, MOS_VERSION))
    if _req(doc, "content_sha256", "document") != content_sha256(doc):
        raise SchemaError("content_sha256 does not match the content (edited by hand or a mixed-up copy)")

    binning = _req(doc, "binning", "document")
    if _req(binning, "kind", "binning") not in BINNING_KINDS:
        raise SchemaError("binning.kind must be one of %r" % (BINNING_KINDS,))
    edges = tuple(_number(e, "binning.edges") for e in _req(binning, "edges", "binning"))
    if len(edges) < 2 or any(a >= b for a, b in zip(edges, edges[1:])):
        raise SchemaError("binning.edges must be strictly increasing with at least 2 entries")
    transform = _parse_transform(_req(doc, "transform", "document"), sv)
    fallback = _req(doc, "fallback", "document")
    unknown_model = _req(fallback, "unknown_model", "fallback")
    lead_oor = _req(fallback, "lead_out_of_range", "fallback")
    if lead_oor not in LEAD_OUT_OF_RANGE:
        raise SchemaError("fallback.lead_out_of_range must be one of %r" % (LEAD_OUT_OF_RANGE,))

    raw_tables = _req(doc, "tables", "document")
    if not isinstance(raw_tables, list) or not raw_tables:
        raise SchemaError("tables must be a non-empty list")
    tables = tuple(_parse_table(t, len(edges), sv) for t in raw_tables)
    ids = [t.table_id for t in tables]
    if len(set(ids)) != len(ids):
        raise SchemaError("table_id must be unique")
    groups = {}
    for t in tables:
        groups.setdefault((t.predictor, t.target, t.model), []).append(t)
    for key, ts in groups.items():
        spans = sorted(t.lead_hours for t in ts)
        if any(a[1] > b[0] for a, b in zip(spans, spans[1:])):
            raise SchemaError("lead_hours overlap for %r" % (key,))
    if unknown_model is not None and not any(t.model == unknown_model for t in tables):
        raise SchemaError("fallback.unknown_model %r has no table" % (unknown_model,))

    reference_p = {}
    if sv >= 2:
        for t in tables:
            where = "table %s" % t.table_id
            src = _req(t.meta["training_source"], "api", where + " training_source")
            if not isinstance(src, str) or not src:
                raise SchemaError("%s: training_source.api must be a non-empty string" % where)
            _int_list(_req(t.meta["training_source"], "levels_hpa", where + " training_source"), 1, 1100,
                      where + " training_source.levels_hpa")
            if transform.kind == "ratio_to_reference":
                norm = t.meta["normalization"]
                p_ref = _number(_req(norm, "p_ref", where + " normalization"), where + " normalization.p_ref")
                if not 0.0 < p_ref <= 1.0:
                    raise SchemaError("%s: normalization.p_ref must be in (0, 1]" % where)
                if not isinstance(_req(norm, "source", where + " normalization"), str):
                    raise SchemaError("%s: normalization.source must be a string" % where)
                reference_p[t.table_id] = p_ref
    elif transform.kind == "ratio_to_reference":
        if not isinstance(transform.reference_bin, int) or not 0 <= transform.reference_bin < len(edges):
            raise SchemaError("transform.reference.bin out of range")
        for key, ts in groups.items():
            ref = [t for t in ts if t.lead_hours == transform.reference_lead_hours]
            if not ref:
                raise SchemaError("no reference table (lead_hours %r) for %r" % (transform.reference_lead_hours, key))
            p_ref = ref[0].p[transform.reference_bin]
            if p_ref <= 0.0:
                raise SchemaError("reference p must be > 0 for %r" % (key,))
            for t in ts:
                reference_p[t.table_id] = p_ref

    return TableSet(table_set_id=_req(doc, "table_set_id", "document"), table_set_version=_req(doc, "table_set_version", "document"),
                    schema_version=sv, edges=edges, transform=transform, unknown_model=unknown_model,
                    lead_out_of_range=lead_oor, tables=tables, reference_p=reference_p, doc=doc)
