"""アメダス JSON の実確認: 地点メタ、全国10分値マップ、遡れる期間、sun1h の有無、唐松岳近傍/高標高地点"""
import json
import math
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

LAT, LON = 36.7595, 137.7503
JST = timezone(timedelta(hours=9))
BASE = "https://www.jma.go.jp/bosai/amedas"
RAW = Path(__file__).parent / "raw"
HDR = {"User-Agent": "yamabiyori-backtest phase0 probe (personal, minimal requests)"}


def get(url):
    time.sleep(1.0)
    r = requests.get(url, headers=HDR, timeout=60)
    print(f"GET {url} -> {r.status_code} {r.headers.get('Content-Type')} {len(r.content)}B")
    return r


# 1) latest_time
r = get(f"{BASE}/data/latest_time.txt")
latest_txt = r.text.strip()
print("latest_time.txt:", latest_txt)
latest = datetime.fromisoformat(latest_txt)

# 2) 地点メタ
r = get(f"{BASE}/const/amedastable.json")
table = r.json()
(RAW / "amedastable.json").write_bytes(r.content)
print("amedastable: 地点数", len(table))
sample_id = next(iter(table))
print("sample entry:", sample_id, json.dumps(table[sample_id], ensure_ascii=False))
elems_seen = set()
for v in table.values():
    elems_seen.add(v.get("elems"))
print("elems 文字列のバリエーション数:", len(elems_seen), " 例:", list(elems_seen)[:5])


def dms(x):
    return x[0] + x[1] / 60.0


def hav(lat1, lon1, lat2, lon2):
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


rows = []
for sid, v in table.items():
    lat, lon = dms(v["lat"]), dms(v["lon"])
    rows.append({"id": sid, "kj": v.get("kjName"), "kn": v.get("knName"), "type": v.get("type"),
                 "lat": round(lat, 4), "lon": round(lon, 4), "alt": v.get("alt"), "elems": v.get("elems"),
                 "dist_km": round(hav(LAT, LON, lat, lon), 1)})

print("\n## 唐松岳(36.7595,137.7503) に近い順 15地点")
for x in sorted(rows, key=lambda r: r["dist_km"])[:15]:
    print(f"  {x['id']} {x['kj']:8s} type={x['type']} alt={x['alt']:5}m dist={x['dist_km']:5.1f}km elems={x['elems']}")

print("\n## 標高の高い順 30地点 (全国)")
for x in sorted(rows, key=lambda r: -(r["alt"] or 0))[:30]:
    print(f"  {x['id']} {x['kj']:8s} type={x['type']} alt={x['alt']:5}m dist={x['dist_km']:6.1f}km elems={x['elems']}")

(RAW / "amedas_stations_sorted.json").write_text(json.dumps(
    {"nearest": sorted(rows, key=lambda r: r["dist_km"])[:30],
     "highest": sorted(rows, key=lambda r: -(r["alt"] or 0))[:60]}, ensure_ascii=False, indent=1), encoding="utf-8")

# 3) 最新の全国10分値マップ
ts = latest.strftime("%Y%m%d%H%M00")
r = get(f"{BASE}/data/map/{ts}.json")
mp = r.json()
(RAW / f"amedas_map_{ts}.json").write_bytes(r.content)
print("map 地点数:", len(mp))
keys = {}
for v in mp.values():
    for k in v:
        keys[k] = keys.get(k, 0) + 1
print("map の要素キーと出現地点数:", keys)
# 唐松岳最寄り数地点の実値
near_ids = [x["id"] for x in sorted(rows, key=lambda r: r["dist_km"])[:5]]
for sid in near_ids:
    print("  ", sid, table[sid]["kjName"], json.dumps(mp.get(sid), ensure_ascii=False))

# 正時のマップ (sun1h が正時にだけ入る可能性を確認)
hour_ts = latest.replace(minute=0).strftime("%Y%m%d%H%M00")
if hour_ts != ts:
    r = get(f"{BASE}/data/map/{hour_ts}.json")
    mp_h = r.json()
    keys_h = {}
    for v in mp_h.values():
        for k in v:
            keys_h[k] = keys_h.get(k, 0) + 1
    print("正時 map の要素キー:", keys_h)

# 4) 遡れる期間: N日前の同時刻マップ
print("\n## 遡り確認 (map, 同時刻)")
for days in [1, 3, 5, 7, 8, 10, 14, 30]:
    t = (latest - timedelta(days=days)).replace(minute=0)
    r = get(f"{BASE}/data/map/{t.strftime('%Y%m%d%H%M00')}.json")
    print(f"  {days}日前 {t}: HTTP {r.status_code}")

# 5) point データ (地点別・時系列) も確認: 3時間ごとのファイル
sid = near_ids[0]
t3 = latest.replace(minute=0)
t3 = t3.replace(hour=(t3.hour // 3) * 3)
r = get(f"{BASE}/data/point/{sid}/{t3.strftime('%Y%m%d_%H')}.json")
if r.status_code == 200:
    pt = r.json()
    (RAW / f"amedas_point_{sid}_{t3.strftime('%Y%m%d_%H')}.json").write_bytes(r.content)
    print("point ファイルの時刻キー数:", len(pt), " 先頭:", list(pt)[:3])
    first = pt[list(pt)[0]]
    print("point 要素キー:", list(first.keys()))
    hour_keys = [k for k in pt if k.endswith("0000") and k[-6:-4] in ("00",)]
    print("  正時レコード例:", {k: pt[k] for k in list(pt)[::6][:2]})
for days in [7, 8, 10]:
    t = (t3 - timedelta(days=days))
    r = get(f"{BASE}/data/point/{sid}/{t.strftime('%Y%m%d_%H')}.json")
    print(f"  point {days}日前: HTTP {r.status_code}")
