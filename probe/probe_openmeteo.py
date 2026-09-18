"""Phase 0 probe: Open-Meteo の3系統APIに最小リクエストを投げ、生レスポンスを保存する。
推測を排し、実レスポンスだけを docs/api-findings.md の根拠にするためのもの。
"""
import json
import sys
import time
from pathlib import Path

import requests

LAT, LON = 36.7595, 137.7503  # 唐松岳付近
RAW = Path(__file__).parent / "raw"
RAW.mkdir(exist_ok=True)
SLEEP = 1.0


def get(tag, url, params):
    time.sleep(SLEEP)
    r = requests.get(url, params=params, timeout=60)
    body = None
    try:
        body = r.json()
    except Exception:
        body = {"_non_json_body": r.text[:500]}
    rec = {"tag": tag, "url": r.url, "status": r.status_code,
           "headers": {k: v for k, v in r.headers.items()
                       if k.lower().startswith(("x-", "retry", "content-type", "ratelimit"))},
           "body": body}
    (RAW / f"{tag}.json").write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    return r.status_code, body


def summarize_hourly(body):
    """hourly の各変数について 非null件数/総数 と先頭の非null値を返す"""
    out = {}
    h = body.get("hourly") or {}
    times = h.get("time", [])
    for k, v in h.items():
        if k == "time":
            continue
        nn = [x for x in v if x is not None]
        first_idx = next((i for i, x in enumerate(v) if x is not None), None)
        out[k] = {"n": len(v), "nonnull": len(nn),
                  "first_nonnull_time": times[first_idx] if first_idx is not None else None,
                  "sample": nn[:3]}
    return out


def report(tag, status, body):
    print(f"\n### {tag}  HTTP {status}")
    if isinstance(body, dict) and body.get("error"):
        print("  ERROR:", body.get("reason"))
        return
    if isinstance(body, dict):
        meta = {k: body.get(k) for k in ("latitude", "longitude", "elevation", "timezone",
                                          "utc_offset_seconds", "generationtime_ms")}
        print("  meta:", meta)
        for k, s in summarize_hourly(body).items():
            print(f"  {k:48s} nonnull {s['nonnull']:4d}/{s['n']:4d}  first={s['first_nonnull_time']}  sample={s['sample']}")
        if "hourly_units" in body:
            print("  units:", body["hourly_units"])


PREV = "https://previous-runs-api.open-meteo.com/v1/forecast"
SINGLE = "https://single-runs-api.open-meteo.com/v1/forecast"
HIST = "https://historical-forecast-api.open-meteo.com/v1/forecast"
FORECAST = "https://api.open-meteo.com/v1/forecast"

VARS = ["temperature_2m", "cloud_cover", "cloud_cover_low", "cloud_cover_mid",
        "cloud_cover_high", "precipitation", "wind_speed_10m", "wind_gusts_10m"]

which = sys.argv[1] if len(sys.argv) > 1 else "all"

if which in ("all", "prev"):
    # 1) Previous Runs API: 変数×previous_dayN の充足状況 (最近の1日)
    for model in ["jma_msm", "ecmwf_ifs025", "ecmwf_ifs"]:
        hourly = []
        for v in VARS:
            hourly.append(v)
            for n in (1, 2, 3, 7, 8):
                hourly.append(f"{v}_previous_day{n}")
        st, body = get(f"prev_{model}_recent", PREV, {
            "latitude": LAT, "longitude": LON, "models": model,
            "hourly": ",".join(hourly), "start_date": "2026-09-10", "end_date": "2026-09-10",
            "timezone": "Asia/Tokyo"})
        report(f"prev_{model}_recent", st, body)

if which in ("all", "prev_start"):
    # 2) Previous Runs API: データ開始時期の当たり (temperature_2m_previous_day1 のみ、軽量)
    for model in ["jma_msm", "ecmwf_ifs025", "ecmwf_ifs"]:
        for d in ["2023-12-15", "2024-01-15", "2024-03-01", "2024-03-20", "2024-06-01"]:
            st, body = get(f"prevstart_{model}_{d}", PREV, {
                "latitude": LAT, "longitude": LON, "models": model,
                "hourly": "temperature_2m_previous_day1,cloud_cover_previous_day1",
                "start_date": d, "end_date": d, "timezone": "Asia/Tokyo"})
            report(f"prevstart_{model}_{d}", st, body)

if which in ("all", "single"):
    # 3) Single Runs API: パラメータ名の当たり
    for model in ["ecmwf_ifs", "ecmwf_ifs025", "jma_msm"]:
        st, body = get(f"single_{model}_run20260901", SINGLE, {
            "latitude": LAT, "longitude": LON, "models": model,
            "hourly": ",".join(VARS), "run": "2026-09-01T00:00", "timezone": "Asia/Tokyo"})
        report(f"single_{model}_run20260901", st, body)
    # 開始時期の当たり
    for model, runs in [("ecmwf_ifs", ["2024-03-13T00:00", "2024-03-14T00:00", "2024-03-15T00:00"]),
                        ("ecmwf_ifs025", ["2024-03-14T00:00", "2026-04-01T00:00", "2026-04-02T00:00"]),
                        ("jma_msm", ["2026-04-01T00:00", "2026-04-02T00:00", "2026-04-03T00:00"])]:
        for run in runs:
            st, body = get(f"singlestart_{model}_{run[:10]}", SINGLE, {
                "latitude": LAT, "longitude": LON, "models": model,
                "hourly": "temperature_2m,cloud_cover", "run": run, "timezone": "Asia/Tokyo"})
            report(f"singlestart_{model}_{run[:10]}", st, body)

if which in ("all", "hist"):
    # 4) Historical Forecast API
    for model in ["jma_msm", "ecmwf_ifs025", "ecmwf_ifs"]:
        st, body = get(f"hist_{model}", HIST, {
            "latitude": LAT, "longitude": LON, "models": model,
            "hourly": ",".join(VARS), "start_date": "2026-09-10", "end_date": "2026-09-10",
            "timezone": "Asia/Tokyo"})
        report(f"hist_{model}", st, body)

if which in ("all", "errors"):
    # 5) エラー形式: 不正パラメータ、不正モデル名、run= を通常APIに
    st, body = get("err_badvar", PREV, {"latitude": LAT, "longitude": LON, "models": "ecmwf_ifs025",
                                        "hourly": "no_such_variable_previous_day1",
                                        "start_date": "2026-09-10", "end_date": "2026-09-10"})
    report("err_badvar", st, body)
    st, body = get("err_badmodel", PREV, {"latitude": LAT, "longitude": LON, "models": "no_such_model",
                                          "hourly": "temperature_2m_previous_day1",
                                          "start_date": "2026-09-10", "end_date": "2026-09-10"})
    report("err_badmodel", st, body)
    st, body = get("err_run_on_forecast", FORECAST, {"latitude": LAT, "longitude": LON,
                                                     "models": "ecmwf_ifs025", "hourly": "temperature_2m",
                                                     "run": "2026-09-01T00:00"})
    report("err_run_on_forecast", st, body)
    st, body = get("err_single_norun", SINGLE, {"latitude": LAT, "longitude": LON,
                                                "models": "ecmwf_ifs025", "hourly": "temperature_2m"})
    report("err_single_norun", st, body)
    st, body = get("err_single_badrun", SINGLE, {"latitude": LAT, "longitude": LON,
                                                 "models": "ecmwf_ifs025", "hourly": "temperature_2m",
                                                 "run": "2026-09-01T03:00"})
    report("err_single_badrun", st, body)
