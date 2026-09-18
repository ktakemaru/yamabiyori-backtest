"""Single Runs API 追加確認: jma_msm の開始日、ホライズン長(forecast_days)、初期時刻の刻み"""
import json
import time
from datetime import date, timedelta
from pathlib import Path

import requests

LAT, LON = 36.7595, 137.7503
SINGLE = "https://single-runs-api.open-meteo.com/v1/forecast"
RAW = Path(__file__).parent / "raw"
log = []


def q(tag, params):
    time.sleep(0.8)
    p = {"latitude": LAT, "longitude": LON, "timezone": "Asia/Tokyo", **params}
    r = requests.get(SINGLE, params=p, timeout=60)
    body = r.json()
    rec = {"tag": tag, "url": r.url, "status": r.status_code, "body": body}
    (RAW / f"{tag}.json").write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    if body.get("error"):
        print(f"{tag}: HTTP {r.status_code} {body.get('reason')}")
        return r.status_code, None
    h = body["hourly"]
    times = h["time"]
    v = h.get("temperature_2m", [])
    nn = [i for i, x in enumerate(v) if x is not None]
    span = f"{times[nn[0]]}..{times[nn[-1]]} ({len(nn)}h nonnull / {len(v)} returned)" if nn else "all null"
    print(f"{tag}: HTTP {r.status_code} {span}")
    return r.status_code, body


def run_ok(model, d):
    st, body = q(f"single_bisect_{model}_{d}", {"models": model, "hourly": "temperature_2m",
                                                "run": f"{d}T00:00"})
    return st == 200


print("## jma_msm 開始日の二分探索 (00Z run)")
lo, hi = date(2026, 4, 3), date(2026, 9, 1)
assert not run_ok("jma_msm", lo)
assert run_ok("jma_msm", hi)
while (hi - lo).days > 1:
    mid = lo + (hi - lo) / 2
    if run_ok("jma_msm", mid):
        hi = mid
    else:
        lo = mid
print(f"=> jma_msm 最初に取得できた run: {hi}T00:00Z")

print("\n## ホライズン長: forecast_days を伸ばすと何時間まで返るか")
for model, fd in [("ecmwf_ifs", 16), ("ecmwf_ifs025", 16), ("jma_msm", 16)]:
    q(f"single_horizon_{model}_fd{fd}", {"models": model, "hourly": "temperature_2m",
                                         "run": "2026-09-01T00:00", "forecast_days": fd})

print("\n## 初期時刻の刻み (00Z以外)")
for model, run in [("ecmwf_ifs", "2026-09-01T12:00"), ("ecmwf_ifs", "2026-09-01T06:00"),
                   ("ecmwf_ifs025", "2026-09-01T12:00"), ("ecmwf_ifs025", "2026-09-01T06:00"),
                   ("jma_msm", "2026-09-01T12:00"), ("jma_msm", "2026-09-01T03:00"),
                   ("jma_msm", "2026-09-01T06:00")]:
    q(f"single_runhour_{model}_{run.replace(':', '')}", {"models": model, "hourly": "temperature_2m", "run": run})

print("\n## 最新に近い run (昨日00Z) と、複数モデル同時指定")
yday = (date.today() - timedelta(days=1)).isoformat()
q(f"single_recent_ecmwf_ifs025", {"models": "ecmwf_ifs025", "hourly": "temperature_2m", "run": f"{yday}T00:00"})
st, body = q("single_multimodel", {"models": "ecmwf_ifs025,jma_msm", "hourly": "temperature_2m",
                                   "run": "2026-09-01T00:00"})
if body:
    print("  hourly keys:", list(body["hourly"].keys()))
