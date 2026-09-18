"""系統2: Forecast API (api.open-meteo.com) のスナップショット収集 (Phase 2)。

Previous Runs API では cloud_cover_low/mid/high の過去リードが全 null (api-findings §1.1) なので、
層別雲量のリード別アーカイブは「今日から」自前で貯めるしかない。6時間ごとのスロット (00/06/12/18 UTC) で
5地点 × 2モデルの 16日先までの予報を、生レスポンスのまま `data/snapshots/forecast/<UTC日>/<HH>Z_<model>.json.gz`
に保存する。取得時刻 (fetched_at) と対象スロットをメタデータに持つので、後段でリードタイムを計算できる。

スロット = 実行時刻の 3 時間前を含む直近の 6 時間境界。cron が数十分遅れてもスロットは変わらない。
同じスロットのファイルが既にあれば取らない (二重取得しない)。取り損ねたスロットは事後には取れない
(Forecast API は「今」しか返さない) ので、欠損としてそのまま残る。

ラン初期時刻は Open-Meteo のメタデータ API `https://api.open-meteo.com/data/<model>/static/meta.json`
(api-findings §10; リクエスト制限にカウントされない) から取る。スナップショットの直前と直後に両モデルの
メタデータを取り、`<HH>Z_meta_before.json.gz` / `<HH>Z_meta_after.json.gz` に生のまま保存する。
直前と直後で last_run_initialisation_time が一致していれば、その間に取ったスナップショットのラン初期時刻は確定。
last_run_modification_time から 10 分経っていなければ (全サーバへの反映待ち、公式推奨) 待ってから取る。

    python -m backtest.collect_forecast_snapshot
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

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
META_URL = "https://api.open-meteo.com/data/{model}/static/meta.json"
META_SETTLE_SECONDS = 600   # last_run_modification_time からこれだけ待つ (公式推奨 10 分)
SNAPSHOT_DIR = config.DATA_DIR / "snapshots" / "forecast"
SLOT_HOURS = 6
SLOT_DELAY_HOURS = 3   # ランの配信遅れを見込んで、実行時刻の3時間前を含むスロットを取る
HOURLY_VARS = [
    "cloud_cover", "cloud_cover_low", "cloud_cover_mid", "cloud_cover_high",
    "precipitation", "precipitation_probability", "temperature_2m", "relative_humidity_2m",
    "wind_speed_10m", "wind_gusts_10m", "cape",
]
FORECAST_DAYS = 16
SLEEP_SECONDS = 2.0


def slot_for(now_utc: datetime) -> datetime:
    t = now_utc.astimezone(timezone.utc) - timedelta(hours=SLOT_DELAY_HOURS)
    return t.replace(minute=0, second=0, microsecond=0, hour=(t.hour // SLOT_HOURS) * SLOT_HOURS)


def snapshot_path(slot: datetime, model: str, snapshot_dir: Path = SNAPSHOT_DIR) -> Path:
    return snapshot_dir / f"{slot:%Y-%m-%d}" / f"{slot:%H}Z_{model}.json.gz"


def build_params(model: str, sites=None) -> dict:
    sites = sites or config.SITES
    return {
        "latitude": ",".join(str(s["lat"]) for s in sites),
        "longitude": ",".join(str(s["lon"]) for s in sites),
        "models": model, "hourly": ",".join(HOURLY_VARS), "forecast_days": FORECAST_DAYS,
        **config.REQUEST_UNITS,
    }


def take_snapshot(session: requests.Session, slot: datetime, model: str, sites=None,
                  snapshot_dir: Path = SNAPSHOT_DIR) -> Path | None:
    """1モデル分を取って保存。既にあれば None。"""
    sites = sites or config.SITES
    path = snapshot_path(slot, model, snapshot_dir)
    if path.exists():
        log.info("skip (exists) %s", path)
        return None
    params = build_params(model, sites)
    r = session.get(FORECAST_URL, params=params, timeout=120)
    body = r.json()
    if r.status_code != 200 or (isinstance(body, dict) and body.get("error")):
        raise RuntimeError(f"forecast API {model}: HTTP {r.status_code} {body}")
    envelope = {
        "slot_utc": slot.isoformat(), "model": model,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sites": [s["site_id"] for s in sites], "site_defs": sites,
        "request": params, "url": r.url, "status": r.status_code, "body": body,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump(envelope, f, ensure_ascii=False)
    tmp.replace(path)
    n = len(body) if isinstance(body, list) else 1
    log.info("saved %s (%d locations)", path, n)
    return path


def meta_path(slot: datetime, phase: str, snapshot_dir: Path = SNAPSHOT_DIR) -> Path:
    return snapshot_dir / f"{slot:%Y-%m-%d}" / f"{slot:%H}Z_meta_{phase}.json.gz"


def fetch_metadata(session: requests.Session, models=None) -> dict:
    """モデルごとの meta.json を生のまま返す。失敗したモデルは {"error": ...}。"""
    out = {}
    for model in models or config.MODELS:
        try:
            r = session.get(META_URL.format(model=model), timeout=30)
            out[model] = r.json() if r.status_code == 200 else {"error": True, "status": r.status_code, "body": r.text[:200]}
        except (requests.RequestException, ValueError) as e:
            out[model] = {"error": True, "reason": str(e)}
    return out


def save_metadata(slot: datetime, phase: str, meta: dict, snapshot_dir: Path = SNAPSHOT_DIR) -> Path:
    path = meta_path(slot, phase, snapshot_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    env = {"slot_utc": slot.isoformat(), "phase": phase,
           "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "models": meta}
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(env, f, ensure_ascii=False)
    return path


def seconds_until_settled(meta: dict, now: datetime = None) -> float:
    """last_run_modification_time + META_SETTLE_SECONDS までの残り秒 (最大値をモデル間で取る)。"""
    now = now or datetime.now(timezone.utc)
    wait = 0.0
    for m in meta.values():
        t = m.get("last_run_modification_time") if isinstance(m, dict) else None
        if isinstance(t, (int, float)):
            settled = datetime.fromtimestamp(t, timezone.utc) + timedelta(seconds=META_SETTLE_SECONDS)
            wait = max(wait, (settled - now).total_seconds())
    return wait


def load_snapshot(path: Path) -> dict:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)


def collect(session: requests.Session = None, now: datetime = None, snapshot_dir: Path = SNAPSHOT_DIR,
            sleep_seconds: float = SLEEP_SECONDS) -> dict:
    session = session or requests.Session()
    slot = slot_for(now or datetime.now(timezone.utc))
    saved, skipped, errors = [], [], []
    todo = [m for m in config.MODELS if not snapshot_path(slot, m, snapshot_dir).exists()]
    if not todo:
        log.info("all snapshots for slot %s exist", slot.isoformat())
        return {"slot_utc": slot.isoformat(), "saved": [], "skipped": list(config.MODELS), "errors": []}
    meta_before = fetch_metadata(session)
    wait = seconds_until_settled(meta_before)
    if wait > 0:
        log.info("last_run_modification_time is recent; waiting %.0fs for all servers to update", wait)
        time.sleep(min(wait, META_SETTLE_SECONDS))
        meta_before = fetch_metadata(session)
    save_metadata(slot, "before", meta_before, snapshot_dir)
    for model in config.MODELS:
        try:
            p = take_snapshot(session, slot, model, snapshot_dir=snapshot_dir)
        except (requests.RequestException, RuntimeError, ValueError) as e:
            log.error("%s: %s", model, e)
            errors.append(f"{model}: {e}")
            continue
        (saved if p else skipped).append(model)
        if p:
            time.sleep(sleep_seconds)
    meta_after = fetch_metadata(session)
    save_metadata(slot, "after", meta_after, snapshot_dir)
    init = {m: (meta_before.get(m, {}).get("last_run_initialisation_time"),
                meta_after.get(m, {}).get("last_run_initialisation_time")) for m in config.MODELS}
    for m, (b, a) in init.items():
        if b != a:
            log.warning("%s: run changed during snapshot (before=%s after=%s); init time ambiguous", m, b, a)
    return {"slot_utc": slot.isoformat(), "saved": saved, "skipped": skipped, "errors": errors,
            "run_init_before_after": init}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--snapshot-dir", type=Path, default=SNAPSHOT_DIR)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    result = collect(snapshot_dir=args.snapshot_dir)
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
