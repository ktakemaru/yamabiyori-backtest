"""Previous Runs API から (model, site, 月) 単位で取得し、生レスポンスを data/raw に保存する。
パースは parse.py に分離。ここでは JSON の中身を一切加工しない。

再実行可能性: 保存済みで完全な生ファイルがある (model, site, 月) はスキップする。
壊れたファイル (JSON でない / error / hourly 無し) は再取得対象になる。"""
import gzip
import json
import logging
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

from . import config

log = logging.getLogger(__name__)


def month_chunks(start: date, end: date) -> list[tuple[date, date]]:
    """[start, end] を暦月で区切った (chunk_start, chunk_end) のリスト (両端含む)。"""
    if start > end:
        return []
    chunks = []
    cur = start
    while cur <= end:
        next_month = (cur.replace(day=1) + timedelta(days=32)).replace(day=1)
        chunk_end = min(end, next_month - timedelta(days=1))
        chunks.append((cur, chunk_end))
        cur = next_month
    return chunks


def raw_path(model: str, site_id: str, chunk_start: date, raw_dir: Path = None) -> Path:
    raw_dir = raw_dir or config.RAW_DIR
    return raw_dir / model / site_id / f"{chunk_start:%Y-%m}.json.gz"


def open_text(path: Path, mode: str = "rt"):
    """'.gz' なら gzip、それ以外は素のテキストとして開く (フィクスチャは非圧縮のまま置いている)。"""
    if ".gz" in Path(path).name:
        return gzip.open(path, mode, encoding="utf-8")
    return open(path, mode, encoding="utf-8")


def load_raw(path: Path) -> dict | None:
    """保存済み生ファイルを読む。壊れていれば None。"""
    try:
        with open_text(path) as f:
            env = json.load(f)
    except (OSError, ValueError, EOFError):
        return None
    body = env.get("body")
    if not isinstance(body, dict) or body.get("error") or "hourly" not in body:
        return None
    if not body["hourly"].get("time"):
        return None
    return env


def is_complete(path: Path) -> bool:
    return path.exists() and load_raw(path) is not None


def build_params(site: dict, model: str, chunk_start: date, chunk_end: date,
                 variables=None, leads=None) -> dict:
    return {
        "latitude": site["lat"], "longitude": site["lon"], "models": model,
        "hourly": config.hourly_param(variables, leads or config.leads_for(model)),
        "start_date": chunk_start.isoformat(), "end_date": chunk_end.isoformat(),
        **config.REQUEST_UNITS,
    }


def request_with_retry(session: requests.Session, params: dict) -> tuple[int, dict, str]:
    """HTTP 429/5xx とタイムアウトは指数バックオフで再試行。4xx (429以外) は即返す。"""
    delay = 5.0
    last_exc = None
    for attempt in range(1, config.MAX_RETRIES + 1):
        try:
            r = session.get(config.PREVIOUS_RUNS_URL, params=params, timeout=config.REQUEST_TIMEOUT)
        except requests.RequestException as e:
            last_exc = e
            log.warning("request failed (attempt %d/%d): %s", attempt, config.MAX_RETRIES, e)
        else:
            if r.status_code == 429 or r.status_code >= 500:
                log.warning("HTTP %d (attempt %d/%d), backing off %.0fs", r.status_code, attempt,
                            config.MAX_RETRIES, delay)
            else:
                try:
                    return r.status_code, r.json(), r.url
                except ValueError:
                    # 実際に観測: HTTP 200 で本文が "Unexpected error while streaming data:
                    # timeoutReached" (サーバ側のストリーミングタイムアウト、jma_msm のコールド
                    # キャッシュで発生)。一時的なので再試行する。
                    last_exc = RuntimeError(f"non-JSON body (HTTP {r.status_code}): {r.text[:200]}")
                    log.warning("%s (attempt %d/%d), backing off %.0fs", last_exc, attempt,
                                config.MAX_RETRIES, delay)
        time.sleep(delay)
        delay = min(delay * 2, 120)
    raise RuntimeError(f"gave up after {config.MAX_RETRIES} attempts: {last_exc}")


def fetch_chunk(session: requests.Session, site: dict, model: str, chunk_start: date, chunk_end: date,
                raw_dir: Path = None, variables=None, leads=None) -> Path:
    """1チャンクを取得して生ファイルに保存。保存済みならスキップしてパスを返す。"""
    path = raw_path(model, site["site_id"], chunk_start, raw_dir)
    if is_complete(path):
        log.info("skip (exists) %s/%s %s", model, site["site_id"], f"{chunk_start:%Y-%m}")
        return path
    params = build_params(site, model, chunk_start, chunk_end, variables, leads)
    status, body, url = request_with_retry(session, params)
    envelope = {
        "site": site, "model": model, "chunk_start": chunk_start.isoformat(), "chunk_end": chunk_end.isoformat(),
        "request": params, "url": url, "status": status,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "body": body,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open_text(tmp, "wt") as f:
        json.dump(envelope, f, ensure_ascii=False)
    if body.get("error"):
        # 保存はするが完全とは見なさない (次回再取得される)。原因調査用。
        tmp.replace(path.with_name(path.name.replace(".json", ".error.json")))
        raise RuntimeError(f"API error for {model}/{site['site_id']} {chunk_start:%Y-%m}: "
                           f"HTTP {status} {body.get('reason')}")
    tmp.replace(path)
    n_hours = len(body["hourly"]["time"])
    log.info("fetched %s/%s %s: %d hours, gen %.0fms", model, site["site_id"], f"{chunk_start:%Y-%m}",
             n_hours, body.get("generationtime_ms", 0))
    return path


def fetch_all(sites: list[dict], models: list[str], start: date, end: date, raw_dir: Path = None,
              sleep_seconds: float = None, session: requests.Session = None) -> tuple[list[Path], list[str]]:
    """全 (model, site, 月) を順に取得。ネットワークに出たときだけ sleep する。
    1チャンクの失敗では止めず、失敗一覧を返す (再実行すれば未取得分だけ取りに行く)。"""
    sleep_seconds = config.SLEEP_SECONDS if sleep_seconds is None else sleep_seconds
    session = session or requests.Session()
    chunks = month_chunks(start, end)
    total = len(models) * len(sites) * len(chunks)
    done = 0
    paths, failures = [], []
    for model in models:
        for site in sites:
            for chunk_start, chunk_end in chunks:
                done += 1
                path = raw_path(model, site["site_id"], chunk_start, raw_dir)
                already = is_complete(path)
                log.info("[%d/%d] %s %s %s", done, total, model, site["site_id"], f"{chunk_start:%Y-%m}")
                try:
                    paths.append(fetch_chunk(session, site, model, chunk_start, chunk_end, raw_dir))
                except RuntimeError as e:
                    log.error("%s", e)
                    failures.append(f"{model}/{site['site_id']}/{chunk_start:%Y-%m}: {e}")
                if not already:
                    time.sleep(sleep_seconds)
    if failures:
        log.error("%d chunk(s) failed; re-run to retry them:\n  %s", len(failures), "\n  ".join(failures))
    return paths, failures
