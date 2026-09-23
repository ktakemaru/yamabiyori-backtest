"""予報値 (と説明変数) → 補正後の値。純粋関数のみ。山頂内挿・モデルの選び方・lead の数え方・スコアは呼び出し側の仕事。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .errors import ContextMismatchError, TableNotFoundError
from .tables import Table, TableSet

ON_MISMATCH_CHOICES = ("error", "passthrough", "allow")


@dataclass(frozen=True)
class Selection:
    table: Optional[Table]     # passthrough で範囲外なら None
    in_scope: bool             # 時刻・月が表の適用範囲内か
    reason: str                # "" / "model_fallback:..." / "lead_nearest" / "out_of_scope:..." を ";" でつないだもの


@dataclass(frozen=True)
class Result:
    value: Optional[float]     # applied=False なら入力値そのもの
    applied: bool
    in_scope: bool
    table_id: Optional[str]
    reason: str


def _lead_distance(t: Table, lead_hours: float) -> float:
    lo, hi = t.lead_hours
    return lo - lead_hours if lead_hours < lo else lead_hours - hi


def select_table(ts: TableSet, *, predictor: str, target: str, model: str, lead_hours: float,
                 hour_jst: int, month: int, on_mismatch: str = "error") -> Selection:
    """使う表を 1 つ選ぶ。

    - (predictor, target) が合う表が無い → TableNotFoundError (on_mismatch と無関係)。
    - model が無い → 表セットの fallback.unknown_model (null なら TableNotFoundError)。
    - lead_hours (ランの初期時刻からの経過時間) が [lo, hi) のどれにも入らない → fallback.lead_out_of_range
      ("nearest_group": 最も近い群、同距離は表の先の行 / "error": TableNotFoundError)。
    - hour_jst (valid_time の JST の時, 0-23) と month (同じく月, 1-12) が表の適用範囲外 → on_mismatch:
      "error" = ContextMismatchError、"passthrough" = table=None を返す、"allow" = その表を in_scope=False で返す。
    """
    if on_mismatch not in ON_MISMATCH_CHOICES:
        raise ValueError("on_mismatch must be one of %r, got %r" % (ON_MISMATCH_CHOICES, on_mismatch))
    if isinstance(hour_jst, bool) or not isinstance(hour_jst, int) or not 0 <= hour_jst <= 23:
        raise ValueError("hour_jst must be an integer 0-23, got %r" % (hour_jst,))
    if isinstance(month, bool) or not isinstance(month, int) or not 1 <= month <= 12:
        raise ValueError("month must be an integer 1-12, got %r" % (month,))

    cands = [t for t in ts.tables if t.predictor == predictor and t.target == target]
    if not cands:
        raise TableNotFoundError("no table for predictor=%r target=%r in %s" % (predictor, target, ts.table_set_id))
    notes = []
    rows = [t for t in cands if t.model == model]
    if not rows:
        rows = [t for t in cands if t.model == ts.unknown_model] if ts.unknown_model is not None else []
        if not rows:
            raise TableNotFoundError("no table for model=%r (and no usable fallback)" % (model,))
        notes.append("model_fallback:%s->%s" % (model, ts.unknown_model))
    table = None
    for t in rows:
        if t.lead_hours[0] <= lead_hours < t.lead_hours[1]:
            table = t
            break
    if table is None:
        if ts.lead_out_of_range == "error":
            raise TableNotFoundError("lead_hours=%r outside every table for model=%r" % (lead_hours, rows[0].model))
        table = min(rows, key=lambda t: _lead_distance(t, lead_hours))
        notes.append("lead_nearest")

    out = []
    if hour_jst not in table.hours_jst:
        out.append("hour_jst=%d not in %s" % (hour_jst, table.meta["time_of_day"]["label"]))
    if month not in table.months:
        out.append("month=%d not in %s" % (month, table.meta["season"]["label"]))
    if out:
        msg = "out_of_scope:" + ",".join(out)
        if on_mismatch == "error":
            raise ContextMismatchError("%s (table %s). Pass on_mismatch='passthrough' or 'allow' only on purpose."
                                       % (msg, table.table_id))
        notes.append(msg)
        if on_mismatch == "passthrough":
            return Selection(table=None, in_scope=False, reason=";".join(notes))
    return Selection(table=table, in_scope=not out, reason=";".join(notes))


def bin_index(edges, value: float) -> int:
    """zero_atom_right_closed: value <= edges[0] → 0、以降 (edges[i-1], edges[i]] → i、最終境界より上 → 最終ビン。"""
    if value <= edges[0]:
        return 0
    for i, hi in enumerate(edges[1:], start=1):
        if value <= hi:
            return i
    return len(edges) - 1


def probability(ts: TableSet, table: Table, value: Optional[float]) -> Optional[float]:
    """予報値 → 事象確率 (表の p)。None は None。"""
    if value is None:
        return None
    return table.p[bin_index(ts.edges, value)]


def effective_value(ts: TableSet, table: Table, value: Optional[float]) -> Optional[float]:
    """表セットの transform に従う出力。
    probability: 確率そのもの。
    ratio_to_reference: round(clamp(scale * (1 - p / p_ref)), round_decimals)、p_ref は同じ (predictor, target, model) の基準表・基準ビン。"""
    p = probability(ts, table, value)
    if p is None or ts.transform.kind == "probability":
        return p
    tr = ts.transform
    p_ref = ts.reference_p[(table.predictor, table.target, table.model)]
    return round(min(tr.clamp[1], max(tr.clamp[0], tr.scale * (1.0 - p / p_ref))), tr.round_decimals)


def calibrate(ts: TableSet, value: Optional[float], *, predictor: str, target: str, model: str, lead_hours: float,
              hour_jst: int, month: int, on_mismatch: str = "error") -> Result:
    """select_table + effective_value。passthrough で範囲外のときは入力値をそのまま返し applied=False。"""
    sel = select_table(ts, predictor=predictor, target=target, model=model, lead_hours=lead_hours,
                       hour_jst=hour_jst, month=month, on_mismatch=on_mismatch)
    if sel.table is None:
        return Result(value=value, applied=False, in_scope=False, table_id=None, reason=sel.reason)
    return Result(value=effective_value(ts, sel.table, value), applied=True, in_scope=sel.in_scope,
                  table_id=sel.table.table_id, reason=sel.reason)
