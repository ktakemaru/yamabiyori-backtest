"""Phase 2 の収集が欠損なく貯まっているかを確認する。

    python -m backtest.check_collection            # 直近7日
    python -m backtest.check_collection --days 3

- アメダス: JST 日ごとに 24 正時が揃っているか、観測所ごとの要素欠損 (null) 数
- 予報スナップショット: UTC 日ごとに 4 スロット × 2 モデルが揃っているか
- アンサンブル (系統3): UTC 日ごとに 00Z ランのメンバー生値と本体確信度の 2 ファイルが揃っているか
  (00Z ENS は 09Z 頃公開なので、当日分は 15Z 以降にだけ期待する)
直近 (実行時刻から SLOT_DELAY+数時間) はまだ取れていなくて正常なので、猶予として除外する。
欠損があれば終了コード 1。
"""
import argparse
import gzip
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config
from .collect_amedas import JST, OBS_DIR, load_day
from .collect_forecast_snapshot import SLOT_DELAY_HOURS, SLOT_HOURS, SNAPSHOT_DIR, slot_for
from . import collect_ensemble, plugin_confidence_snapshot

ENSEMBLE_EXPECT_AFTER_HOUR = 15   # 00Z ENS の公開 (約 09Z) + ジョブ 2 回分の猶予


def check_amedas(days: int, now: datetime, obs_dir: Path = OBS_DIR) -> tuple[list[str], list[str]]:
    lines, problems = [], []
    now_jst = now.astimezone(JST)
    last_expected = now_jst.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    first = (last_expected - timedelta(days=days)).replace(hour=0)
    # 収集開始前の日は期待しない (最初のファイルの日から数える)
    existing = sorted(obs_dir.glob("*.json.gz")) if obs_dir.exists() else []
    if not existing:
        return ["  (no amedas data yet)"], ["amedas: no data files"]
    first_file_day = datetime.strptime(existing[0].name[:10], "%Y-%m-%d").replace(tzinfo=JST)
    first = max(first, first_file_day)
    null_counts: dict[str, dict[str, int]] = {}
    t = first
    while t <= last_expected:
        day = load_day(obs_dir / f"{t:%Y-%m-%d}.json.gz")
        hours = day["hours"]
        expected = [f"{h:02d}00" for h in range(24) if t.replace(hour=h) <= last_expected]
        missing = [h for h in expected if h not in hours]
        lines.append(f"  {t:%Y-%m-%d}: {len(expected) - len(missing):2d}/{len(expected):2d} hours"
                     + (f"  missing {missing}" if missing else ""))
        if missing:
            problems.append(f"amedas {t:%Y-%m-%d}: missing hours {missing}")
        for h, stations in hours.items():
            for sid in config.AMEDAS_STATIONS:
                rec = stations.get(sid)
                if rec is None:
                    null_counts.setdefault(sid, {}).setdefault("(station absent)", 0)
                    null_counts[sid]["(station absent)"] += 1
                    continue
                for e in config.AMEDAS_ELEMENTS:
                    v = rec.get(e)
                    if v is None or v[0] is None:
                        null_counts.setdefault(sid, {}).setdefault(e, 0)
                        null_counts[sid][e] += 1
        t += timedelta(days=1)
    if null_counts:
        lines.append("  null/absent element counts (expected for stations that don't observe that element):")
        for sid, counts in sorted(null_counts.items()):
            lines.append(f"    {sid} {config.AMEDAS_STATIONS.get(sid, '')}: {counts}")
    return lines, problems


def check_snapshots(days: int, now: datetime, snapshot_dir: Path = SNAPSHOT_DIR) -> tuple[list[str], list[str]]:
    lines, problems = [], []
    last_slot = slot_for(now)
    first_slot = last_slot - timedelta(days=days)
    existing = sorted(snapshot_dir.rglob("*.json.gz")) if snapshot_dir.exists() else []
    if not existing:
        return ["  (no snapshots yet)"], ["snapshots: no data files"]
    p0 = existing[0]   # <YYYY-MM-DD>/<HH>Z_<model>.json.gz、ソート順で最古
    earliest = datetime.strptime(f"{p0.parent.name}T{p0.name[:2]}", "%Y-%m-%dT%H").replace(tzinfo=timezone.utc)
    first_slot = max(first_slot, earliest)
    slot = first_slot
    by_day: dict[str, list[str]] = {}
    while slot <= last_slot:
        for model in config.MODELS:
            p = snapshot_dir / f"{slot:%Y-%m-%d}" / f"{slot:%H}Z_{model}.json.gz"
            if p.exists():
                by_day.setdefault(f"{slot:%Y-%m-%d}", []).append(f"{slot:%H}Z/{model}")
            else:
                problems.append(f"snapshot missing {slot:%Y-%m-%d} {slot:%H}Z {model}")
                by_day.setdefault(f"{slot:%Y-%m-%d}", []).append(f"{slot:%H}Z/{model}:MISSING")
        slot += timedelta(hours=SLOT_HOURS)
    for d, items in sorted(by_day.items()):
        ok = sum(1 for i in items if not i.endswith("MISSING"))
        lines.append(f"  {d}: {ok}/{len(items)}  " + " ".join(i for i in items if i.endswith("MISSING")))
    return lines, problems


def check_ensemble(days: int, now: datetime, ens_dir: Path = collect_ensemble.SNAPSHOT_DIR,
                   conf_dir: Path = plugin_confidence_snapshot.SNAPSHOT_DIR) -> tuple[list[str], list[str]]:
    lines, problems = [], []
    last_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if now.hour < ENSEMBLE_EXPECT_AFTER_HOUR:
        last_day -= timedelta(days=1)
    existing = sorted(ens_dir.rglob("00Z_*.json.gz")) if ens_dir.exists() else []
    if not existing:
        return ["  (no 00Z ensemble snapshots yet; expected from the first 09:20Z/12:20Z job after deployment)"], []
    earliest = datetime.strptime(existing[0].parent.name, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    day = max(last_day - timedelta(days=days - 1), earliest)
    while day <= last_day:
        run = day   # 00Z
        items = []
        for label, p in [("members", collect_ensemble.snapshot_path(run, ens_dir)),
                         ("confidence", plugin_confidence_snapshot.snapshot_path(run, conf_dir))]:
            if p.exists():
                items.append(label)
            else:
                items.append(f"{label}:MISSING")
                problems.append(f"ensemble missing {day:%Y-%m-%d} 00Z {label}")
        lines.append(f"  {day:%Y-%m-%d} 00Z: " + " ".join(items))
        day += timedelta(days=1)
    return lines, problems


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--obs-dir", type=Path, default=OBS_DIR)
    ap.add_argument("--snapshot-dir", type=Path, default=SNAPSHOT_DIR)
    args = ap.parse_args(argv)
    now = datetime.now(timezone.utc)
    print(f"check_collection at {now.isoformat(timespec='seconds')} (last {args.days} days)")
    print("## amedas (JST days, hourly maps)")
    l1, p1 = check_amedas(args.days, now, args.obs_dir)
    print("\n".join(l1))
    print(f"## forecast snapshots (UTC, {24 // SLOT_HOURS} slots/day x {len(config.MODELS)} models, "
          f"slots newer than {SLOT_DELAY_HOURS}h ago not expected yet)")
    l2, p2 = check_snapshots(args.days, now, args.snapshot_dir)
    print("\n".join(l2))
    print(f"## ensemble members + plugin confidence (UTC, 00Z run; today's expected only after {ENSEMBLE_EXPECT_AFTER_HOUR}Z)")
    l3, p3 = check_ensemble(args.days, now)
    print("\n".join(l3))
    problems = p1 + p2 + p3
    if problems:
        print(f"\n!! {len(problems)} problem(s):")
        for p in problems:
            print("  ", p)
        return 1
    print("\nOK: no gaps")
    return 0


if __name__ == "__main__":
    sys.exit(main())
