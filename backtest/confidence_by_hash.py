"""本体の確信度スナップショット (系統3b) を **本体の git hash 別** に集計し、版の違いによるスコアの動きを追う。

背景: 本体は 2026-09-19 に v1.3.0 (a65656e/d8d44cf) → v1.4.0 (87c9c3c, R8: 降水閾値) → v1.5.0 (7c9e8f6, R1: 雲量較正) と
3 段階で変わった。確信度 (`compute_ensemble_confidence_by_day`) に効くのは R8 の `ENSEMBLE_PRECIP_WET_THRESHOLD_MM` 0.1→0.2
(precip_confidence / precip_prob が変わる)。R1 の雲量較正は確信度の経路 (気圧面のメンバー雲量) には入らない。
年明けの確信度較正では「較正対象がどの版の出力か」を混同しないこと。

スナップショットには本体の hash・dirty・定数・**入力の生レスポンス** (ensemble_body) が入っているので、
  1. hash 別の一覧 (件数・期間・定数)                                  → `summary`
  2. 同じ (地点, 対象日) が複数の hash で記録されていれば横並び          → `crosshash`  (ただしランが違えば予報自体も違う)
  3. 保存済みの入力から **今の本体で再計算** して、保存値との差 = 版差 (予報差を含まない) → `recompute --plugin-dir ...`
ができる。年明けの較正では (3) で全スナップショットを較正対象の版に揃えるのが正しい。

    python -m backtest.confidence_by_hash summary
    python -m backtest.confidence_by_hash crosshash
    python -m backtest.confidence_by_hash recompute --plugin-dir C:/mountain-weather-r8
"""
import argparse
import sys
from pathlib import Path

import polars as pl

from . import config
from .collect_ensemble import load_gz
from .plugin_confidence_snapshot import SNAPSHOT_DIR, load_plugin, plugin_identity

pl.Config.set_tbl_rows(200)
pl.Config.set_tbl_width_chars(220)
FIELDS = ["confidence", "cloud_confidence", "precip_confidence", "temp_confidence", "cloud_pct", "precip_prob", "precip_mm"]


def load_rows(snapshot_dir: Path = SNAPSHOT_DIR) -> pl.DataFrame:
    rows = []
    for p in sorted(snapshot_dir.rglob("*.json.gz")):
        e = load_gz(p)
        pid = e.get("plugin", {})
        for s in e["sites"]:
            for day, c in s["confidence_by_day"].items():
                rows.append({"file": str(p.relative_to(snapshot_dir)), "run_utc": e["run_utc"], "fetched_at": e["fetched_at"],
                             "plugin_hash": (pid.get("git_hash") or "")[:7], "plugin_hash_full": pid.get("git_hash"),
                             "plugin_dirty": pid.get("git_dirty_tracked"), "plugin_version": pid.get("plugin_version"),
                             "precip_threshold": e.get("plugin_constants", {}).get("ENSEMBLE_PRECIP_WET_THRESHOLD_MM"),
                             "site_id": s["site_id"], "target_day": day, **{k: c.get(k) for k in FIELDS}})
    return pl.DataFrame(rows)


def summary(df: pl.DataFrame) -> pl.DataFrame:
    return (df.group_by(["plugin_hash", "plugin_version", "precip_threshold", "plugin_dirty"])
              .agg(pl.col("file").n_unique().alias("n_snapshots"), pl.col("run_utc").min().alias("first_run"),
                   pl.col("run_utc").max().alias("last_run"), pl.len().alias("n_site_days"))
              .sort("first_run"))


def crosshash(df: pl.DataFrame) -> pl.DataFrame:
    """同じ (site, target_day) が 2 つ以上の hash にある行だけを、hash ごとの confidence / precip_prob で横持ちに。
    ランが違えば入力の予報も違うので、版差と予報差が混ざる (純粋な版差は recompute)。"""
    multi = df.group_by(["site_id", "target_day"]).agg(pl.col("plugin_hash").n_unique().alias("nh")).filter(pl.col("nh") >= 2)
    if multi.is_empty():
        return pl.DataFrame()
    d = df.join(multi.select("site_id", "target_day"), on=["site_id", "target_day"])
    return (d.select("site_id", "target_day", "plugin_hash", "run_utc", "confidence", "precip_confidence", "precip_prob")
             .sort(["site_id", "target_day", "run_utc"]))


def recompute(snapshot_dir: Path, plugin_dir: Path) -> pl.DataFrame:
    """保存済み入力 (ensemble_body, daily_sunrise) を今の本体に食わせて再計算し、保存値との差を出す。
    CONFIDENCE_MIN_DAYS_OUT の cutoff は date.today() 依存なので、両方に存在する日だけ比べる。"""
    core, detail = load_plugin(plugin_dir)
    ident = plugin_identity(plugin_dir)
    rows = []
    for p in sorted(snapshot_dir.rglob("*.json.gz")):
        e = load_gz(p)
        stored_hash = (e.get("plugin", {}).get("git_hash") or "")[:7]
        for s in e["sites"]:
            body = s.get("ensemble_body")
            if not body:
                continue
            detail.fetch_ensemble = lambda lat, lon, hourly, days=detail.FORECAST_DAYS, force_refresh=False, _b=body: _b
            mtn = s["mountain"]
            out = detail.compute_ensemble_confidence_by_day(mtn["lat"], mtn["lon"], s["cloud_var"], s["temp_var"], s["daily_sunrise"])
            for day, c in s["confidence_by_day"].items():
                if day not in out:
                    continue
                rows.append({"file": str(p.relative_to(snapshot_dir)), "site_id": s["site_id"], "target_day": day,
                             "stored_hash": stored_hash, "recomputed_hash": (ident.get("git_hash") or "")[:7],
                             **{f"{k}_stored": c.get(k) for k in FIELDS}, **{f"{k}_new": out[day].get(k) for k in FIELDS}})
    d = pl.DataFrame(rows)
    if d.is_empty():
        return d
    for k in FIELDS:
        d = d.with_columns((pl.col(f"{k}_new") - pl.col(f"{k}_stored")).alias(f"{k}_delta"))
    return d


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["summary", "crosshash", "recompute"])
    ap.add_argument("--snapshot-dir", type=Path, default=SNAPSHOT_DIR)
    ap.add_argument("--plugin-dir", type=Path, default=Path("C:/mountain-weather"))
    args = ap.parse_args(argv)
    if args.mode == "recompute":
        d = recompute(args.snapshot_dir, args.plugin_dir)
        if d.is_empty():
            print("(no rows)"); return 0
        print(d.select("site_id", "target_day", "stored_hash", "recomputed_hash", "confidence_stored", "confidence_new", "confidence_delta",
                       "precip_confidence_delta", "precip_prob_delta", "cloud_confidence_delta", "temp_confidence_delta"))
        print(d.group_by(["stored_hash", "recomputed_hash"]).agg(pl.len().alias("n"), pl.col("confidence_delta").mean().alias("mean_conf_delta"),
              pl.col("precip_prob_delta").mean().alias("mean_precip_prob_delta"), pl.col("cloud_confidence_delta").abs().max().alias("max_abs_cloud_delta")))
        return 0
    df = load_rows(args.snapshot_dir)
    if df.is_empty():
        print("(no snapshots)"); return 0
    print(summary(df) if args.mode == "summary" else (crosshash(df) if not crosshash(df).is_empty() else "(no (site, day) recorded under 2+ hashes yet)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
