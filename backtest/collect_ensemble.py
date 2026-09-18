"""系統3: Open-Meteo Ensemble API (ecmwf_ifs025, 50 メンバー + コントロール) の全メンバー生値を日次で蓄積する。

背景 (docs/api-findings.md §11): アンサンブルは Previous Runs / Single Runs に無く、Ensemble API 自体も
過去 3〜4 日分しか返さない (リード別アーカイブ無し)。したがって確信度 (スプレッド) の検証データは
今から貯めるしかない。スプレッド等の統計はここでは計算せず、生値をそのまま保存する (後で何を評価するか
決められるように)。

保存単位は「ラン」: メタデータ API (`.../data/ecmwf_ifs025_ensemble/static/meta.json`) の
`last_run_initialisation_time` が RUN_HOURS (既定 00Z のみ = 日次) のランで、まだ保存していなければ取る。
Forecast API と違いラン単位で保存するので、ファイルからリード時間が厳密に決まる。
    data/snapshots/ensemble/<ラン日 UTC>/<HH>Z_ecmwf_ifs025_members.json.gz

内容: 5 地点 (config.SITES; 唐松岳は本体 MOUNTAINS と座標が 0.01° 違うが 0.25° 格子では同じセル) × VARS × 51 列 × 3 時間刻み (temporal_resolution=native;
ECMWF ENS の時間解像度は 144h まで 3h、以降 6h を Open-Meteo が補間) × 16 日。1 ファイル ≈ 0.65MB (gz)。
precipitation は native では 3 時間積算 (hourly は 3 等分して 0.1mm 丸め) なので、本体の「毎時 0.1mm 以上」
判定を厳密に再現するには plugin_confidence_snapshot.py が保存する hourly 生値を使う。
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
from .collect_forecast_snapshot import META_SETTLE_SECONDS, META_URL

log = logging.getLogger(__name__)

ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
MODEL = "ecmwf_ifs025"
META_MODEL = "ecmwf_ifs025_ensemble"      # メタデータ API 上の名前 (api-findings §11.1)
SNAPSHOT_DIR = config.DATA_DIR / "snapshots" / "ensemble"
RUN_HOURS = [0]                           # 既定は 00Z のみ (日次)。--run-hours 0,12 で増やせる
FORECAST_DAYS = 16
LEVELS_HPA = [925, 850, 700, 600]         # Ensemble API で値が入る面のうち山頂内挿に使う範囲 (900/800/1000 は不要 or null)
LEVEL_VARS = [f"{k}_{lv}hPa" for lv in LEVELS_HPA for k in ("cloud_cover", "relative_humidity", "temperature", "geopotential_height")]
HOURLY_VARS = ["precipitation", "cloud_cover", "cloud_cover_low", "cloud_cover_mid", "cloud_cover_high", "temperature_2m"] + LEVEL_VARS


def run_from_meta(meta: dict) -> datetime | None:
    t = meta.get("last_run_initialisation_time") if isinstance(meta, dict) else None
    return datetime.fromtimestamp(t, timezone.utc) if isinstance(t, (int, float)) else None


def snapshot_path(run: datetime, snapshot_dir: Path = SNAPSHOT_DIR) -> Path:
    return snapshot_dir / f"{run:%Y-%m-%d}" / f"{run:%H}Z_{MODEL}_members.json.gz"


def build_params(sites=None) -> dict:
    sites = sites or config.SITES
    return {
        "latitude": ",".join(str(s["lat"]) for s in sites),
        "longitude": ",".join(str(s["lon"]) for s in sites),
        "models": MODEL, "hourly": ",".join(HOURLY_VARS), "forecast_days": FORECAST_DAYS,
        "temporal_resolution": "native", **config.REQUEST_UNITS,
    }


def fetch_meta(session: requests.Session) -> dict:
    try:
        r = session.get(META_URL.format(model=META_MODEL), timeout=30)
        return r.json() if r.status_code == 200 else {"error": True, "status": r.status_code, "body": r.text[:200]}
    except (requests.RequestException, ValueError) as e:
        return {"error": True, "reason": str(e)}


RATE_LIMIT_WAIT_SECONDS = 65   # 無料枠の分当たり上限 (600 call) は固定 1 分窓なので、429 なら次の窓まで待つ
RATE_LIMIT_MAX_WAITS = 3


def get_with_rate_limit_retry(session: requests.Session, url: str, params: dict, timeout: int):
    """HTTP 429 (Minutely API request limit exceeded) なら RATE_LIMIT_WAIT_SECONDS 待って再試行。
    members リクエスト 1 本が重み 641 call (api-findings §11.5) で分当たり 600 を超えるため、直後のリクエストは 429 になる。"""
    for i in range(RATE_LIMIT_MAX_WAITS + 1):
        r = session.get(url, params=params, timeout=timeout)
        if r.status_code != 429 or i == RATE_LIMIT_MAX_WAITS:
            return r
        log.info("HTTP 429 (rate limit); waiting %ds (%d/%d)", RATE_LIMIT_WAIT_SECONDS, i + 1, RATE_LIMIT_MAX_WAITS)
        time.sleep(RATE_LIMIT_WAIT_SECONDS)
    return r


def save_gz(path: Path, obj: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    tmp.replace(path)
    return path


def load_gz(path: Path) -> dict:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)


def take_snapshot(session: requests.Session, run: datetime, meta_before: dict, sites=None,
                  snapshot_dir: Path = SNAPSHOT_DIR) -> Path:
    sites = sites or config.SITES
    params = build_params(sites)
    r = get_with_rate_limit_retry(session, ENSEMBLE_URL, params, timeout=300)
    try:
        body = r.json()
    except ValueError:
        raise RuntimeError(f"ensemble API: HTTP {r.status_code} non-JSON {r.text[:200]}")
    if r.status_code != 200 or (isinstance(body, dict) and body.get("error")):
        raise RuntimeError(f"ensemble API: HTTP {r.status_code} {body}")
    meta_after = fetch_meta(session)
    envelope = {
        "run_utc": run.isoformat(), "model": MODEL,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sites": [s["site_id"] for s in sites], "site_defs": sites,
        "request": params, "url": r.url, "status": r.status_code,
        "meta_before": meta_before, "meta_after": meta_after, "body": body,
    }
    if run_from_meta(meta_after) != run:
        log.warning("run changed during fetch (before=%s after=%s)", run, run_from_meta(meta_after))
        envelope["run_ambiguous"] = True
    return save_gz(snapshot_path(run, snapshot_dir), envelope)


def collect(session: requests.Session = None, run_hours=None, snapshot_dir: Path = SNAPSHOT_DIR,
            now: datetime = None) -> dict:
    """メタデータの最新ランが run_hours のランで未保存なら取る。返り値 {run_utc, action, path}。"""
    session = session or requests.Session()
    run_hours = run_hours or RUN_HOURS
    now = now or datetime.now(timezone.utc)
    meta = fetch_meta(session)
    run = run_from_meta(meta)
    if run is None:
        raise RuntimeError(f"metadata unavailable: {meta}")
    if run.hour not in run_hours:
        log.info("latest run %s not in run_hours %s; nothing to do", run.isoformat(), run_hours)
        return {"run_utc": run.isoformat(), "action": "skip_run_hour", "path": None}
    path = snapshot_path(run, snapshot_dir)
    if path.exists():
        log.info("skip (exists) %s", path)
        return {"run_utc": run.isoformat(), "action": "skip_exists", "path": str(path)}
    mod = meta.get("last_run_modification_time")
    if isinstance(mod, (int, float)):
        wait = (datetime.fromtimestamp(mod, timezone.utc) + timedelta(seconds=META_SETTLE_SECONDS) - now).total_seconds()
        if wait > 0:
            log.info("run %s modified recently; waiting %.0fs", run.isoformat(), wait)
            time.sleep(min(wait, META_SETTLE_SECONDS))
            meta = fetch_meta(session)
            if run_from_meta(meta) != run:
                return {"run_utc": run.isoformat(), "action": "skip_run_changed_while_waiting", "path": None}
    p = take_snapshot(session, run, meta, snapshot_dir=snapshot_dir)
    log.info("saved %s", p)
    return {"run_utc": run.isoformat(), "action": "saved", "path": str(p)}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--snapshot-dir", type=Path, default=SNAPSHOT_DIR)
    ap.add_argument("--run-hours", default=",".join(str(h) for h in RUN_HOURS), help="保存対象のラン初期時刻 (UTC, カンマ区切り)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    result = collect(run_hours=[int(h) for h in args.run_hours.split(",")], snapshot_dir=args.snapshot_dir)
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
