"""気圧面変数(本体の標高内挿の入力)が Previous Runs / Single Runs で取れるか。ついでに cape / precipitation_probability も。"""
import json
import time
from pathlib import Path

import requests

LAT, LON = 36.7595, 137.7503
PREV = "https://previous-runs-api.open-meteo.com/v1/forecast"
SINGLE = "https://single-runs-api.open-meteo.com/v1/forecast"
RAW = Path(__file__).parent / "raw"

LEVEL_KINDS = ["cloud_cover", "relative_humidity", "wind_speed", "temperature", "geopotential_height"]
LEVELS = [925, 850, 700]


def summarize(tag, r):
    body = r.json()
    (RAW / f"{tag}.json").write_text(json.dumps({"url": r.url, "status": r.status_code, "body": body},
                                                 ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n### {tag} HTTP {r.status_code}")
    if body.get("error"):
        print("  ERROR:", body["reason"])
        return
    for k, v in body["hourly"].items():
        if k == "time":
            continue
        nn = [x for x in v if x is not None]
        print(f"  {k:48s} nonnull {len(nn):4d}/{len(v):4d} sample={nn[:2]}")


for model in ["jma_msm", "ecmwf_ifs025", "ecmwf_ifs"]:
    hourly = []
    for kind in LEVEL_KINDS:
        for lv in LEVELS:
            hourly.append(f"{kind}_{lv}hPa_previous_day1")
    hourly += ["cape_previous_day1", "precipitation_probability_previous_day1",
               "cape", "precipitation_probability", "cloud_cover_850hPa"]
    time.sleep(1)
    r = requests.get(PREV, params={"latitude": LAT, "longitude": LON, "models": model, "hourly": ",".join(hourly),
                                   "start_date": "2026-09-10", "end_date": "2026-09-10", "timezone": "Asia/Tokyo"},
                     timeout=90)
    summarize(f"prev_levels_{model}", r)

for model in ["jma_msm", "ecmwf_ifs025", "ecmwf_ifs"]:
    hourly = [f"{kind}_{lv}hPa" for kind in LEVEL_KINDS for lv in LEVELS] + ["cape", "precipitation_probability"]
    time.sleep(1)
    r = requests.get(SINGLE, params={"latitude": LAT, "longitude": LON, "models": model, "hourly": ",".join(hourly),
                                     "run": "2026-09-01T00:00", "timezone": "Asia/Tokyo"}, timeout=90)
    summarize(f"single_levels_{model}", r)
