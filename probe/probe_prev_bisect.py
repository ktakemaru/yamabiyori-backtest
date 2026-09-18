"""Previous Runs API: モデル別のデータ開始日を二分探索で絞る (temperature_2m_previous_day1 の非null有無)"""
import json
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import requests

LAT, LON = 36.7595, 137.7503
PREV = "https://previous-runs-api.open-meteo.com/v1/forecast"
RAW = Path(__file__).parent / "raw"
log = []


def has_data(model, d, var="temperature_2m_previous_day1"):
    time.sleep(0.8)
    r = requests.get(PREV, params={"latitude": LAT, "longitude": LON, "models": model,
                                   "hourly": var, "start_date": d.isoformat(),
                                   "end_date": d.isoformat(), "timezone": "Asia/Tokyo"}, timeout=60)
    body = r.json()
    vals = (body.get("hourly") or {}).get(var, [])
    nn = sum(1 for x in vals if x is not None)
    log.append({"model": model, "date": d.isoformat(), "status": r.status_code, "nonnull": nn, "n": len(vals)})
    print(f"  {model} {d} -> nonnull {nn}/{len(vals)}")
    return nn > 0


def bisect(model, lo, hi):
    """lo: データ無し(と期待), hi: データ有り(と期待)。境界を日単位まで詰める"""
    assert not has_data(model, lo), f"{model}: {lo} にデータあり、lo を早めて"
    assert has_data(model, hi), f"{model}: {hi} にデータなし、hi を遅らせて"
    while (hi - lo).days > 1:
        mid = lo + (hi - lo) / 2
        if has_data(model, mid):
            hi = mid
        else:
            lo = mid
    return hi


targets = {
    "jma_msm": (date(2021, 1, 1), date(2023, 12, 15)),
    "ecmwf_ifs025": (date(2024, 1, 15), date(2024, 3, 1)),
    "ecmwf_ifs": (date(2024, 6, 1), date(2026, 9, 10)),
}
only = sys.argv[1:] or list(targets)
result = {}
for model in only:
    lo, hi = targets[model]
    print(f"## {model}")
    try:
        first = bisect(model, lo, hi)
        result[model] = first.isoformat()
        print(f"  => {model} 最初にデータのある日: {first}")
    except AssertionError as e:
        print("  !!", e)
        result[model] = f"unresolved: {e}"

(RAW / "prev_bisect_log.json").write_text(json.dumps({"result": result, "log": log}, ensure_ascii=False, indent=1), encoding="utf-8")
print(result)
