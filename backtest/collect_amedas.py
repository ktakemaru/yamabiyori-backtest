"""系統1: アメダス実況の日次収集 (Phase 2)。

気象庁の全国10分値マップ `data/map/yyyyMMddHH0000.json` (毎正時) を取り、config.AMEDAS_STATIONS の
観測所 × config.AMEDAS_ELEMENTS だけを抜き出して `data/obs/amedas/<JST日付>.json.gz` に追記する。
map は約8日で 404 になる (api-findings §5) ので、毎回「保持窓内でまだ持っていない正時」を全部取りに行く
= 失敗しても次回で埋まる。同じ正時を二度は取らない。

    python -m backtest.collect_amedas            # 保持窓内の未取得正時を全部取る
    python -m backtest.collect_amedas --max-requests 10
"""
import argparse
import gzip
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from . import config

log = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))
AMEDAS_BASE = "https://www.jma.go.jp/bosai/amedas"
OBS_DIR = config.DATA_DIR / "obs" / "amedas"
RETENTION_HOURS = 8 * 24 - 2   # api-findings §5: 8日前は 200、10日前は 404。境界は安全側に
HEADERS = {"User-Agent": "yamabiyori-backtest collector (personal research; ~8 requests/day)"}
SLEEP_SECONDS = 1.5


def fetch_latest_time(session: requests.Session) -> datetime:
    r = session.get(f"{AMEDAS_BASE}/data/latest_time.txt", headers=HEADERS, timeout=30)
    r.raise_for_status()
    return datetime.fromisoformat(r.text.strip()).astimezone(JST)


def map_url(t: datetime) -> str:
    return f"{AMEDAS_BASE}/data/map/{t.astimezone(JST):%Y%m%d%H%M}00.json"


def extract(map_json: dict, stations=None, elements=None) -> dict:
    """全国マップから対象観測所・要素だけ抜く。値は JMA の [値, 品質フラグ] のまま。"""
    stations = stations or config.AMEDAS_STATIONS
    elements = elements or config.AMEDAS_ELEMENTS
    out = {}
    for sid in stations:
        rec = map_json.get(sid)
        if rec is None:
            continue
        out[sid] = {e: rec[e] for e in elements if e in rec}
    return out


def day_path(t: datetime, obs_dir: Path = OBS_DIR) -> Path:
    return obs_dir / f"{t.astimezone(JST):%Y-%m-%d}.json.gz"


def load_day(path: Path) -> dict:
    if not path.exists():
        return {"date_jst": path.name[:10], "stations": dict(config.AMEDAS_STATIONS),
                "elements": list(config.AMEDAS_ELEMENTS), "hours": {}}
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)


def save_day(path: Path, day: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump(day, f, ensure_ascii=False, sort_keys=True)
    tmp.replace(path)


def hour_key(t: datetime) -> str:
    return f"{t.astimezone(JST):%H%M}"


def wanted_hours(latest: datetime, retention_hours: int = None) -> list[datetime]:
    """保持窓内の毎正時 (JST)。古い順。"""
    retention_hours = RETENTION_HOURS if retention_hours is None else retention_hours
    end = latest.replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(hours=retention_hours)
    hours = []
    t = start
    while t <= end:
        hours.append(t)
        t += timedelta(hours=1)
    return hours


def missing_hours(hours: list[datetime], obs_dir: Path = OBS_DIR) -> list[datetime]:
    cache = {}
    out = []
    for t in hours:
        p = day_path(t, obs_dir)
        if p not in cache:
            cache[p] = load_day(p)["hours"]
        if hour_key(t) not in cache[p]:
            out.append(t)
    return out


def collect(session: requests.Session = None, obs_dir: Path = OBS_DIR, max_requests: int = 250,
            sleep_seconds: float = SLEEP_SECONDS, now: datetime = None) -> dict:
    session = session or requests.Session()
    latest = fetch_latest_time(session) if now is None else now
    todo = missing_hours(wanted_hours(latest), obs_dir)
    log.info("latest_time=%s, missing hours in retention window: %d", latest.isoformat(), len(todo))
    fetched, not_found, errors = 0, 0, 0
    days: dict[Path, dict] = {}
    # 新しい方から取る: 古い側は次回でも 404 になる可能性が高く、新しい側は今しか取れない
    for t in sorted(todo, reverse=True)[:max_requests]:
        url = map_url(t)
        try:
            r = session.get(url, headers=HEADERS, timeout=60)
        except requests.RequestException as e:
            log.warning("%s: %s", url, e)
            errors += 1
            time.sleep(sleep_seconds)
            continue
        if r.status_code == 404:
            not_found += 1
            log.info("404 %s", url)
        elif r.status_code != 200:
            errors += 1
            log.warning("HTTP %d %s", r.status_code, url)
        else:
            p = day_path(t, obs_dir)
            day = days.setdefault(p, load_day(p))
            day["hours"][hour_key(t)] = extract(r.json())
            fetched += 1
            log.info("fetched %s (%d stations)", t.isoformat(), len(day["hours"][hour_key(t)]))
        time.sleep(sleep_seconds)
    for p, day in days.items():
        save_day(p, day)
    return {"latest_time": latest.isoformat(), "missing_before": len(todo), "fetched": fetched,
            "not_found": not_found, "errors": errors, "days_written": [str(p) for p in days]}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--max-requests", type=int, default=250)
    ap.add_argument("--obs-dir", type=Path, default=OBS_DIR)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    result = collect(obs_dir=args.obs_dir, max_requests=args.max_requests)
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
