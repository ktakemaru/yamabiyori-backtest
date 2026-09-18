"""本体 (ヤマビヨリ) の確信度スコアを、本体をライブラリとして読み込んで日次で記録する。

本体は一切変更しない:
  - `sys.dont_write_bytecode = True` にしてから import する (本体ディレクトリに __pycache__ を作らない)
  - 本体の fetch_ensemble() は本体ディレクトリ配下の cache/ に書き込むので、**このプロセス内でだけ**
    同じリクエストをキャッシュ無しで行う関数に差し替える (モジュール属性の置換。ファイルは触らない)。
  - 本体の compute_ensemble_confidence_by_day() をそのまま呼ぶ。入力は本体と同じ
    (nearest_pressure_level(標高) の cloudcover/temperature + precipitation、Asia/Tokyo、14 日、hourly)。

記録するもの (data/snapshots/plugin_confidence/<ラン日 UTC>/<HH>Z.json.gz):
  - 本体の git hash (`git rev-parse HEAD`)、ブランチ、作業ツリーが clean か、
    mountain_weather_detail.py / mountain_weather_core.py の sha256
  - 本体の定数 (ENSEMBLE_MODEL, ENSEMBLE_MEMBER_COUNT, CONFIDENCE_MIN_DAYS_OUT, 閾値, 窓の定数)
  - 地点ごとに: 本体 MOUNTAINS のエントリ、使った変数名、日の出 (Forecast API daily=sunrise; 本体と同じ出所)、
    アンサンブル API の生レスポンス (本体の関数の入力そのもの)、本体の出力 (日別 confidence と内訳)
  - 対応するアンサンブルラン (メタデータ API の last_run_initialisation_time)
  - `date.today()` (本体の CONFIDENCE_MIN_DAYS_OUT の基準日はローカル日付。Actions では TZ=Asia/Tokyo で実行)

本体の場所は --plugin-dir か環境変数 YAMABIYORI_PLUGIN_DIR (既定 C:/mountain-weather)。
"""
import argparse
import gzip
import hashlib
import json
import logging
import os
import subprocess
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

import requests

from . import config
from .collect_ensemble import ENSEMBLE_URL, RUN_HOURS, fetch_meta, run_from_meta, save_gz

log = logging.getLogger(__name__)

DEFAULT_PLUGIN_DIR = Path(os.environ.get("YAMABIYORI_PLUGIN_DIR", "C:/mountain-weather"))
SNAPSHOT_DIR = config.DATA_DIR / "snapshots" / "plugin_confidence"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
PLUGIN_FILES = ["mountain_weather_detail.py", "mountain_weather_core.py", "mountain_terrain.py"]
SLEEP_SECONDS = 2.0


def plugin_identity(plugin_dir: Path) -> dict:
    """本体の git hash 等。git が無い/失敗した場合は None を入れて続行 (sha256 は常に取る)。"""
    def git(*args):
        try:
            return subprocess.run(["git", "-C", str(plugin_dir), *args], capture_output=True, text=True, timeout=30,
                                  check=True).stdout.strip()
        except (subprocess.SubprocessError, FileNotFoundError):
            return None
    status = git("status", "--porcelain", "--untracked-files=no")
    return {
        "plugin_dir": str(plugin_dir),
        "git_hash": git("rev-parse", "HEAD"),
        "git_branch": git("rev-parse", "--abbrev-ref", "HEAD"),
        "git_dirty_tracked": (None if status is None else bool(status)),
        "git_dirty_files": (None if not status else status.splitlines()),
        "sha256": {f: hashlib.sha256((plugin_dir / f).read_bytes()).hexdigest() for f in PLUGIN_FILES if (plugin_dir / f).exists()},
    }


def load_plugin(plugin_dir: Path):
    """本体を読み取り専用で import。戻り値 (core, detail)。"""
    sys.dont_write_bytecode = True
    if str(plugin_dir) not in sys.path:
        sys.path.insert(0, str(plugin_dir))
    import mountain_weather_core as core      # noqa: E402
    import mountain_weather_detail as detail  # noqa: E402
    return core, detail


def make_nocache_fetch(session: requests.Session, detail, store: dict):
    """本体 fetch_ensemble() と同じリクエストをキャッシュ無しで行い、生レスポンスを store に残す差し替え関数。"""
    def fetch_ensemble(lat, lon, hourly, days=detail.FORECAST_DAYS, force_refresh=False):
        params = {"latitude": lat, "longitude": lon, "forecast_days": days,
                  "timezone": "Asia/Tokyo", "models": detail.ENSEMBLE_MODEL, "hourly": hourly}
        r = session.get(ENSEMBLE_URL, params=params, timeout=300)
        body = r.json()
        if r.status_code != 200 or body.get("error"):
            raise requests.exceptions.RequestException(f"ensemble API HTTP {r.status_code}: {body}")
        store["request"] = {**params, "hourly": list(hourly)}
        store["url"] = r.url
        store["body"] = body
        return body
    return fetch_ensemble


def fetch_sunrise(session: requests.Session, mountains: list, days: int) -> list[dict]:
    """本体 fetch_forecast() と同じ出所 (Forecast API の daily sunrise/sunset, Asia/Tokyo) を 1 リクエストで。"""
    params = {"latitude": ",".join(str(m["lat"]) for m in mountains), "longitude": ",".join(str(m["lon"]) for m in mountains),
              "daily": "sunrise,sunset", "timezone": "Asia/Tokyo", "forecast_days": days}
    r = session.get(FORECAST_URL, params=params, timeout=120)
    body = r.json()
    if r.status_code != 200 or (isinstance(body, dict) and body.get("error")):
        raise RuntimeError(f"forecast API (sunrise): HTTP {r.status_code} {body}")
    return body if isinstance(body, list) else [body]


def snapshot_path(run: datetime, snapshot_dir: Path = SNAPSHOT_DIR) -> Path:
    return snapshot_dir / f"{run:%Y-%m-%d}" / f"{run:%H}Z.json.gz"


def snapshot(plugin_dir: Path = DEFAULT_PLUGIN_DIR, sites=None, session: requests.Session = None,
             snapshot_dir: Path = SNAPSHOT_DIR, run: datetime = None, force: bool = False, run_hours=None) -> dict:
    session = session or requests.Session()
    sites = sites or config.SITES
    core, detail = load_plugin(plugin_dir)
    meta = fetch_meta(session)
    run = run or run_from_meta(meta)
    if run is None:
        raise RuntimeError(f"metadata unavailable: {meta}")
    if run.hour not in (run_hours or RUN_HOURS):
        log.info("latest run %s not in run_hours; nothing to do", run.isoformat())
        return {"run_utc": run.isoformat(), "action": "skip_run_hour", "path": None}
    path = snapshot_path(run, snapshot_dir)
    if path.exists() and not force:
        log.info("skip (exists) %s", path)
        return {"run_utc": run.isoformat(), "action": "skip_exists", "path": str(path)}

    by_name = {m["name"]: m for m in core.MOUNTAINS}
    mountains = []
    for s in sites:
        if s["name"] not in by_name:
            raise KeyError(f"{s['name']} not in plugin MOUNTAINS")
        mountains.append(by_name[s["name"]])
    sunrise_bodies = fetch_sunrise(session, mountains, detail.FORECAST_DAYS)
    time.sleep(SLEEP_SECONDS)

    results = []
    for s, mtn, sun in zip(sites, mountains, sunrise_bodies):
        daily_sunrise = dict(zip(sun["daily"]["time"], sun["daily"]["sunrise"]))
        hpa = core.nearest_pressure_level(mtn["elevation_m"])
        cloud_var, temp_var = f"cloudcover_{hpa}hPa", f"temperature_{hpa}hPa"
        store = {}
        detail.fetch_ensemble = make_nocache_fetch(session, detail, store)   # プロセス内の差し替えのみ
        out = detail.compute_ensemble_confidence_by_day(mtn["lat"], mtn["lon"], cloud_var, temp_var, daily_sunrise)
        results.append({
            "site_id": s["site_id"], "mountain": mtn, "pressure_level_hpa": hpa, "cloud_var": cloud_var, "temp_var": temp_var,
            "daily_sunrise": daily_sunrise, "ensemble_request": store.get("request"), "ensemble_url": store.get("url"),
            "ensemble_body": store.get("body"), "confidence_by_day": out,
        })
        log.info("%s: %d days, e.g. %s", s["site_id"], len(out), next(iter(out.items())) if out else None)
        time.sleep(SLEEP_SECONDS)

    envelope = {
        "run_utc": run.isoformat(), "meta": meta,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "today_local": date.today().isoformat(), "tz_env": os.environ.get("TZ"),
        "plugin": plugin_identity(plugin_dir),
        "plugin_constants": {k: getattr(detail, k) for k in
                             ["ENSEMBLE_MODEL", "ENSEMBLE_MEMBER_COUNT", "CONFIDENCE_MIN_DAYS_OUT", "ENSEMBLE_PRECIP_WET_THRESHOLD_MM",
                              "FORECAST_DAYS", "TRIP_START_OFFSET_HOURS", "MAIN_TIME_END_HOUR", "RIDGE_DWELL_TRIM_HOURS"]
                             if hasattr(detail, k)},
        "sites": results,
    }
    save_gz(path, envelope)
    log.info("saved %s", path)
    return {"run_utc": run.isoformat(), "action": "saved", "path": str(path)}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--plugin-dir", type=Path, default=DEFAULT_PLUGIN_DIR)
    ap.add_argument("--snapshot-dir", type=Path, default=SNAPSHOT_DIR)
    ap.add_argument("--force", action="store_true", help="同じランのファイルがあっても取り直す")
    ap.add_argument("--run-hours", default=",".join(str(h) for h in RUN_HOURS), help="対象ラン初期時刻 (UTC, カンマ区切り; collect_ensemble と揃える)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    result = snapshot(plugin_dir=args.plugin_dir, snapshot_dir=args.snapshot_dir, force=args.force,
                      run_hours=[int(h) for h in args.run_hours.split(",")])
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
