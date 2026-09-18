"""Ensemble API (/v1/ensemble) の実測プローブ。

確認すること:
  1. ecmwf_ifs025 / jma_msm (他にどのモデル名が通るか) のメンバー数・ホライズン
  2. cloud_cover (全/低/中/高)、気圧面 (RH/GPH/cloud_cover @ 1000..600hPa) の可否
  3. 過去アーカイブ: past_days / start_date で遡れる範囲、
     previous-runs-api / single-runs-api がアンサンブルモデル名・member 列を受け付けるか
  4. 1 リクエストのサイズ・所要時間 (Phase 2 の負荷見積り用)
"""
import json
import sys
import time
from pathlib import Path

import requests

RAW = Path(__file__).parent / "raw"
ENS = "https://ensemble-api.open-meteo.com/v1/ensemble"
PREV = "https://previous-runs-api.open-meteo.com/v1/forecast"
SINGLE = "https://single-runs-api.open-meteo.com/v1/forecast"
LAT, LON = 36.7595, 137.7503   # 唐松岳
LEVELS = [1000, 925, 900, 850, 800, 700, 600]


def get(tag, url, params):
    t0 = time.time()
    r = requests.get(url, params=params, timeout=180)
    dt = time.time() - t0
    try:
        body = r.json()
    except ValueError:
        body = {"_non_json": r.text[:300]}
    (RAW / f"ens_{tag}.json").write_text(json.dumps({"url": r.url, "status": r.status_code, "elapsed_s": round(dt, 2),
                                                     "bytes": len(r.content), "body": body}, ensure_ascii=False), encoding="utf-8")
    print(f"\n### {tag} HTTP {r.status_code} {len(r.content)/1024:.0f}KB {dt:.1f}s gen={body.get('generationtime_ms') if isinstance(body, dict) else None}")
    if isinstance(body, dict) and body.get("error"):
        print("  ERROR:", body["reason"])
        return None
    if "_non_json" in body:
        print("  NON-JSON:", body["_non_json"])
        return None
    return body


def summarize_hourly(body):
    h = body.get("hourly", {})
    times = h.get("time", [])
    print(f"  time: {len(times)} steps {times[0] if times else None} .. {times[-1] if times else None}")
    bases = {}
    for k, v in h.items():
        if k == "time":
            continue
        base = k.split("_member")[0]
        nn = sum(1 for x in v if x is not None)
        bases.setdefault(base, []).append((k, nn, len(v)))
    for base, cols in sorted(bases.items()):
        members = [c for c in cols if "_member" in c[0]]
        ctrl = [c for c in cols if "_member" not in c[0]]
        nn = [c[1] for c in cols]
        # 最後の非 null インデックス (ホライズン)
        last = max((max((i for i, x in enumerate(h[c[0]]) if x is not None), default=-1) for c in cols), default=-1)
        print(f"  {base:34s} ctrl={len(ctrl)} members={len(members)} nonnull {min(nn)}..{max(nn)}/{cols[0][2]} last_nonnull_t={times[last] if last >= 0 else None}")
    return bases


def main(which=None):
    which = which or ["models", "vars", "archive", "prev_single"]
    if "models" in which:
        # モデル名の総当たり (本体は ecmwf_ifs025; jma_msm の有無、他候補)
        for m in ["ecmwf_ifs025", "ecmwf_ifs04", "jma_msm", "jma_gsm", "gfs025", "gfs05", "icon_global", "gem_global", "bom_access_global"]:
            time.sleep(1.5)
            b = get(f"model_{m}", ENS, {"latitude": LAT, "longitude": LON, "models": m, "hourly": "cloud_cover,precipitation",
                                          "forecast_days": 16, "timezone": "UTC"})
            if b:
                summarize_hourly(b)
    if "vars" in which:
        hourly = ["cloud_cover", "cloud_cover_low", "cloud_cover_mid", "cloud_cover_high", "precipitation", "wind_speed_10m",
                  "temperature_2m", "relative_humidity_2m", "cape", "freezing_level_height"]
        hourly += [f"{k}_{lv}hPa" for k in ["relative_humidity", "geopotential_height", "cloud_cover", "temperature", "wind_speed"] for lv in LEVELS]
        for m in ["ecmwf_ifs025"]:
            time.sleep(1.5)
            b = get(f"vars_{m}", ENS, {"latitude": LAT, "longitude": LON, "models": m, "hourly": ",".join(hourly),
                                         "forecast_days": 16, "timezone": "UTC", "wind_speed_unit": "ms"})
            if b:
                summarize_hourly(b)
            # 1 つずつ落として不可変数を特定 (まとめて 400 の場合)
            if b is None:
                for v in hourly:
                    time.sleep(1.0)
                    bb = get(f"var1_{m}_{v}", ENS, {"latitude": LAT, "longitude": LON, "models": m, "hourly": v, "forecast_days": 2, "timezone": "UTC"})
                    print("   ->", v, "OK" if bb else "NG")
    if "archive" in which:
        # 過去にどこまで遡れるか
        for label, extra in [("past7", {"past_days": 7}), ("past30", {"past_days": 30}), ("past92", {"past_days": 92}),
                             ("range_2026-09-01", {"start_date": "2026-09-01", "end_date": "2026-09-03"}),
                             ("range_2026-06-01", {"start_date": "2026-06-01", "end_date": "2026-06-03"}),
                             ("range_2026-01-10", {"start_date": "2026-01-10", "end_date": "2026-01-12"})]:
            time.sleep(1.5)
            b = get(f"archive_{label}", ENS, {"latitude": LAT, "longitude": LON, "models": "ecmwf_ifs025", "hourly": "cloud_cover",
                                              "timezone": "UTC", **extra})
            if b:
                summarize_hourly(b)
    if "prev_single" in which:
        for label, url, params in [
            ("prev_model", PREV, {"models": "ecmwf_ifs025", "hourly": "cloud_cover_member01_previous_day1,cloud_cover_previous_day1", "start_date": "2026-09-10", "end_date": "2026-09-10"}),
            ("prev_ensmodel", PREV, {"models": "ecmwf_ifs025_ensemble", "hourly": "cloud_cover_previous_day1", "start_date": "2026-09-10", "end_date": "2026-09-10"}),
            ("single_member", SINGLE, {"models": "ecmwf_ifs025", "hourly": "cloud_cover_member01,cloud_cover", "run": "2026-09-01T00:00"}),
            ("single_ensmodel", SINGLE, {"models": "ecmwf_ifs025_ensemble", "hourly": "cloud_cover", "run": "2026-09-01T00:00"}),
            ("ens_run_param", ENS, {"models": "ecmwf_ifs025", "hourly": "cloud_cover", "run": "2026-09-01T00:00", "forecast_days": 3}),
        ]:
            time.sleep(1.5)
            b = get(label, url, {"latitude": LAT, "longitude": LON, "timezone": "UTC", **params})
            if b:
                summarize_hourly(b)
    # メタデータ
    for m in ["ecmwf_ifs025_ensemble", "ecmwf_ifs025", "jma_msm_ensemble"]:
        time.sleep(1)
        r = requests.get(f"https://api.open-meteo.com/data/{m}/static/meta.json", timeout=60)
        print(f"\n### meta {m} HTTP {r.status_code}: {r.text[:400]}")
    # ensemble-api 側のメタ
    for m in ["ecmwf_ifs025_ensemble", "ecmwf_ifs025"]:
        time.sleep(1)
        r = requests.get(f"https://ensemble-api.open-meteo.com/data/{m}/static/meta.json", timeout=60)
        print(f"\n### ens-meta {m} HTTP {r.status_code}: {r.text[:400]}")


if __name__ == "__main__":
    main(sys.argv[1:] or None)
