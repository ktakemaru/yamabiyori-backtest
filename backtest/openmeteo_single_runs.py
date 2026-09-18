"""Track B (Phase 3): Single Runs API から気圧面変数を「1ラン × 全検証地点」単位で取得し、生レスポンスを
data/raw/single_runs/<model>/<YYYY-MM-DD>T<HH>Z.json.gz に保存する。パースは parse_single_runs.py。

実測 (api-findings §10): 1リクエスト (5地点, 21列) は初回 30〜110 秒、サーバ側の cold read で HTTP 500 や
HTTP 200 + 非JSON (timeoutReached) が出るが、同じランを再要求すると数秒で返る。→ 長めのタイムアウトと
再試行で 1 ランずつ warm up させる。取得済みランはスキップ (再開可能)。
"""
import gzip
import json
import logging
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

from . import config

log = logging.getLogger(__name__)

SINGLE_RUNS_URL = "https://single-runs-api.open-meteo.com/v1/forecast"
RAW_DIR = config.DATA_DIR / "raw" / "single_runs"

# 本体 core.LEVEL_STACK_HPA は [1000,925,900,850,800,700,600] だが、Single Runs では 900/800hPa が
# 両モデルとも全 null (api-findings §10) なので要求しない。
LEVELS_HPA = [1000, 925, 850, 700, 600]
LEVEL_KINDS = ["relative_humidity", "geopotential_height", "cloud_cover"]
SURFACE_VARS = ["cloud_cover", "cloud_cover_low", "cloud_cover_mid", "cloud_cover_high",
                "relative_humidity_2m", "temperature_2m"]
FORECAST_DAYS = {"ecmwf_ifs025": 10, "jma_msm": 4}
RUN_HOURS = [0, 12]
# api-findings §2.1: Single Runs の最古のラン
FIRST_RUN_DATE = {"ecmwf_ifs025": date(2026, 4, 2), "jma_msm": date(2026, 5, 13)}
SLEEP_SECONDS = 2.0
REQUEST_TIMEOUT = 240
MAX_RETRIES = 10


def hourly_vars() -> list[str]:
    return [f"{k}_{lv}hPa" for lv in LEVELS_HPA for k in LEVEL_KINDS] + SURFACE_VARS


def raw_path(model: str, run: datetime, raw_dir: Path = None) -> Path:
    raw_dir = raw_dir or RAW_DIR
    return raw_dir / model / f"{run:%Y-%m-%dT%H}Z.json.gz"


def load_raw(path: Path) -> dict | None:
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            env = json.load(f)
    except (OSError, ValueError, EOFError):
        return None
    body = env.get("body")
    if not isinstance(body, list) or not body or any("hourly" not in loc for loc in body):
        return None
    return env


def is_complete(path: Path) -> bool:
    return path.exists() and load_raw(path) is not None


def build_params(model: str, run: datetime, sites=None) -> dict:
    sites = sites or config.SITES
    return {
        "latitude": ",".join(str(s["lat"]) for s in sites),
        "longitude": ",".join(str(s["lon"]) for s in sites),
        "models": model, "hourly": ",".join(hourly_vars()),
        "run": f"{run:%Y-%m-%dT%H:%M}", "forecast_days": FORECAST_DAYS[model],
        "timezone": "UTC",
    }


def request_with_retry(session: requests.Session, params: dict) -> tuple[int, object, str]:
    delay = 10.0
    last = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = session.get(SINGLE_RUNS_URL, params=params, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as e:
            last = e
            log.warning("request failed (attempt %d/%d): %s", attempt, MAX_RETRIES, e)
        else:
            try:
                body = r.json()
            except ValueError:
                body = None
            if r.status_code == 200 and body is not None:
                return r.status_code, body, r.url
            if r.status_code == 400:
                return r.status_code, body, r.url   # ラン無し等、再試行しても無駄
            text = r.text or ""
            if "modelRunUnavailable" in text:
                # 実測: アーカイブに無いランは HTTP 200 + 非JSON本文
                # "Unexpected error while streaming data: modelRunUnavailable(...)" で返る。再試行しても無駄。
                return 400, {"error": True, "reason": text[:200]}, r.url
            last = RuntimeError(f"HTTP {r.status_code} {text[:120]}")
            log.warning("%s (attempt %d/%d), backing off %.0fs", last, attempt, MAX_RETRIES, delay)
        time.sleep(delay)
        delay = min(delay * 1.3, 45)   # サーバ側の cold read が warm up するまで待つ
    raise RuntimeError(f"gave up after {MAX_RETRIES} attempts: {last}")


def fetch_run(session: requests.Session, model: str, run: datetime, sites=None, raw_dir: Path = None) -> Path | None:
    """1ランを取得して保存。保存済みならスキップ (None を返す)。ランが存在しない (400) なら .missing を残す。"""
    path = raw_path(model, run, raw_dir)
    if is_complete(path):
        return None
    missing_marker = path.with_name(path.name.replace(".json.gz", ".missing"))
    if missing_marker.exists():
        return None
    sites = sites or config.SITES
    params = build_params(model, run, sites)
    t0 = time.time()
    status, body, url = request_with_retry(session, params)
    if status == 400:
        reason = body.get("reason") if isinstance(body, dict) else str(body)
        log.warning("run not available: %s %s: %s", model, run, reason)
        missing_marker.parent.mkdir(parents=True, exist_ok=True)
        missing_marker.write_text(json.dumps({"reason": reason, "url": url}), encoding="utf-8")
        return None
    envelope = {"model": model, "run_utc": run.isoformat(), "sites": [s["site_id"] for s in sites],
                "site_defs": sites, "request": params, "url": url, "status": status,
                "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "body": body}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump(envelope, f, ensure_ascii=False)
    tmp.replace(path)
    gen = body[0].get("generationtime_ms", 0) if isinstance(body, list) and body else 0
    log.info("fetched %s %s: %.0fs wall, gen %.0fms", model, f"{run:%Y-%m-%dT%H}Z", time.time() - t0, gen)
    return path


def runs_between(start: date, end: date, hours=None) -> list[datetime]:
    hours = hours or RUN_HOURS
    out = []
    d = start
    while d <= end:
        for h in hours:
            out.append(datetime(d.year, d.month, d.day, h, tzinfo=timezone.utc))
        d += timedelta(days=1)
    return out


def fetch_all(models: list[str], start: date, end: date, hours=None, raw_dir: Path = None,
              sleep_seconds: float = SLEEP_SECONDS, session: requests.Session = None) -> tuple[int, list[str]]:
    session = session or requests.Session()
    fetched, failures = 0, []
    for model in models:
        runs = [r for r in runs_between(max(start, FIRST_RUN_DATE.get(model, start)), end, hours)]
        for i, run in enumerate(runs, 1):
            log.info("[%s %d/%d] %s", model, i, len(runs), f"{run:%Y-%m-%dT%H}Z")
            try:
                p = fetch_run(session, model, run, raw_dir=raw_dir)
            except RuntimeError as e:
                log.error("%s", e)
                failures.append(f"{model}/{run:%Y-%m-%dT%H}Z: {e}")
                continue
            if p is not None:
                fetched += 1
                time.sleep(sleep_seconds)
    if failures:
        log.error("%d run(s) failed; re-run to retry:\n  %s", len(failures), "\n  ".join(failures))
    return fetched, failures
