"""R1 の較正表を mos の新形式 (schema "yamabiyori-mos-table" v1) で書き出す。

数値は cloud_calibration_table.build() をそのまま使い (同じ parquet から再計算)、既存の docs/cloud-calibration-table.json と
n・p_sunny・p_sunny_used が一致することを確認してから書く (一致しなければ止まる。--allow-legacy-diff で続行し identical=false を記録)。
既存の cloud_calibration_table.py と docs/cloud-calibration-table.{json,txt} は変更しない。

新形式で足したもの:
- lead を経過時間 [lo, hi) で持つ (学習の lead_day = lead_hours // 24 + 1 なので d{lo}-{hi} → [24(lo−1), 24·hi))
- 適用範囲を数値で持つ: 時刻 = trackb_eval.DAY_HOURS_JST、月 = 標本の valid_time (JST) に現れた月
- 地点別の n・日数と、日数 PROVISIONAL_MIN_DAYS 未満の地点を provisional として区別した検証済み標高
- provenance (生成スクリプト、コミット、作業ツリーが dirty か、関係ファイルの sha256) と content_sha256

    python -m backtest.mos_export [--out mos_tables/r1-summit-cloud-sunny.json]
"""
import argparse
import hashlib
import json
import subprocess
from datetime import date, timedelta
from pathlib import Path

import polars as pl

from mos import MOS_VERSION, SCHEMA_NAME, content_sha256, parse_table_set

from . import config
from .cloud_calibration_table import BIN_EDGES, BIN_LABELS, LEAD_GROUPS, OUT_JSON as LEGACY_JSON, build
from .trackb_eval import DAY_HOURS_JST, OBS_PATH, SR_PATH, SUN_SUNNY_H, lead_day_pairs

REPO = config.DATA_DIR.parent
OUT = REPO / "mos_tables" / "r1-summit-cloud-sunny.json"
TABLE_SET_ID = "r1-summit-cloud-sunny"
TABLE_SET_VERSION = "1.0.0"
PREDICTOR = "cloud_cover_at_summit"
TARGET = "p_sunny"
PROVISIONAL_MIN_DAYS = 30        # 地点の標本が 30 日 (JST の valid 日付の数) 未満なら provisional。根拠は docs/mos-module-design.md §2.3
SOURCE_FILES = ["backtest/cloud_calibration_table.py", "backtest/trackb_eval.py", "backtest/quantile_map.py",
                "backtest/mos_export.py", "backtest/config.py"]
SITE_BY_ID = {s["site_id"]: s for s in config.SITES}


def _git(*args) -> str | None:
    try:
        r = subprocess.run(["git", "--no-optional-locks", "-C", str(REPO), *args], capture_output=True, text=True, timeout=30)
    except (subprocess.SubprocessError, FileNotFoundError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def git_info() -> dict:
    status = _git("status", "--porcelain")
    return {"commit": _git("rev-parse", "HEAD"), "dirty": None if status is None else bool(status),
            "legacy_json_commit": _git("log", "-1", "--format=%H", "--", "docs/cloud-calibration-table.json")}


def source_sha256() -> dict:
    return {p: hashlib.sha256((REPO / p).read_bytes()).hexdigest() for p in SOURCE_FILES if (REPO / p).exists()}


def sample(sr: pl.DataFrame, obs: pl.DataFrame) -> pl.DataFrame:
    """build() と同じ組 (fuji 除外) に JST の日付・時・月と lead 群を付ける。"""
    d = lead_day_pairs(sr, obs, PREDICTOR).filter(~pl.col("pair").str.starts_with("fuji"))
    jst = pl.col("valid_time").dt.convert_time_zone("Asia/Tokyo")
    d = d.with_columns(jst.dt.date().alias("date_jst"), jst.dt.hour().alias("hour_jst"), jst.dt.month().alias("month_jst"),
                       pl.col("pair").str.split("/").list.first().alias("site_id"))
    group = pl.lit(None, dtype=pl.String)
    for model, groups in LEAD_GROUPS.items():
        for lo, hi in groups:
            group = (pl.when((pl.col("model") == model) & pl.col("lead_day").is_between(lo, hi)).then(pl.lit(f"d{lo}-{hi}"))
                     .otherwise(group))
    return d.with_columns(group.alias("lead_group"))


def validated_elevation(g: pl.DataFrame) -> dict:
    s = g.group_by("site_id").agg(pl.len().alias("n"), pl.col("date_jst").n_unique().alias("days")).sort("site_id")
    sites = []
    for site_id, n, days in s.iter_rows():
        site = SITE_BY_ID[site_id]
        sites.append({"site_id": site_id, "name": site["name"], "elevation_m": site["elevation_m"],
                      "station_id": site["station_id"], "station_name": site["station_name"], "n": n, "days": days,
                      "share": round(n / g.height, 3), "status": "confirmed" if days >= PROVISIONAL_MIN_DAYS else "provisional"})
    conf = [x["elevation_m"] for x in sites if x["status"] == "confirmed"]
    every = [x["elevation_m"] for x in sites]
    return {"provisional_rule": {"min_days": PROVISIONAL_MIN_DAYS, "days": "distinct JST dates of valid_time in this table's sample"},
            "confirmed_m": [min(conf), max(conf)] if conf else None,
            "including_provisional_m": [min(every), max(every)],
            "sites": sites}


def build_doc(sr: pl.DataFrame, obs: pl.DataFrame, git: dict, today: date, legacy: dict | None) -> dict:
    table, _ = build(sr, obs)
    d = sample(sr, obs)
    hours_seen = sorted(set(d["hour_jst"].to_list()))
    if hours_seen != sorted(DAY_HOURS_JST):
        raise RuntimeError(f"sample hours {hours_seen} != DAY_HOURS_JST {DAY_HOURS_JST}")
    identical = legacy is not None and legacy_diffs(table, legacy) == []
    provenance = {"generator": "backtest/cloud_calibration_table.py (build)", "exporter": "backtest/mos_export.py",
                  "commit": git["commit"], "dirty": git["dirty"], "exported": today.isoformat(),
                  "source_sha256": source_sha256(),
                  "legacy_json": {"path": "docs/cloud-calibration-table.json", "commit": git["legacy_json_commit"],
                                  "built": legacy.get("built") if legacy else None, "identical": identical}}
    tables = []
    for model, groups in table.items():
        for entry in groups:
            lo, hi = entry["lead_days"]
            g = d.filter((pl.col("model") == model) & (pl.col("lead_group") == f"d{lo}-{hi}"))
            if g.height != entry["n"]:
                raise RuntimeError(f"{model} d{lo}-{hi}: sample n {g.height} != build n {entry['n']}")
            dates = g["date_jst"]
            runs = g["run_utc"]
            used_stations = [SITE_BY_ID[s] for s in sorted(set(g["site_id"].to_list()))]
            tables.append({
                "table_id": f"{model}/h{24 * (lo - 1)}-{24 * hi}",
                "predictor": PREDICTOR, "predictor_unit": "%",
                "predictor_definition": "Open-Meteo pressure-level cloud cover (1000-600hPa) interpolated to the summit elevation "
                                        "linearly in geopotential height each hour (same as the plugin's add_altitude_columns)",
                "target": TARGET,
                "target_definition": f"P(sun1h >= {SUN_SUNNY_H} h in the hour ending at valid_time)",
                "model": model,
                "lead_hours": [24 * (lo - 1), 24 * hi],
                "lead_definition": "hours from the run's initial time to valid_time; the table covers [lo, hi). "
                                   "Training grouped lead_day = lead_hours // 24 + 1",
                "lead_days_legacy": [lo, hi],
                "time_of_day": {"label": "day", "basis": "clock_jst_hour_ending", "hours_jst": sorted(DAY_HOURS_JST),
                                "source": "backtest/trackb_eval.py DAY_HOURS_JST (hour ending HH:00 JST)"},
                "season": {"label": "warm", "basis": "valid_month_jst", "months": sorted(set(g["month_jst"].to_list())),
                           "sampled_valid_dates_jst": [dates.min().isoformat(), dates.max().isoformat()],
                           "source": "months present in the training sample (no season filter in the training code; "
                                     "tracka_eval.WARM_MONTHS = 4-9 is a different analysis and is not used here)"},
                "observation": {
                    "kind": "foot_amedas_sun1h",
                    "pairing": "summit forecast vs the foot station's sunshine (representativeness error included)",
                    "event": f"sun1h >= {SUN_SUNNY_H} h",
                    "sources_preference": "etrn_cache preferred over amedas_map at the same hour (trackb_eval.obs_wide)",
                    "stations": [{"station_id": s["station_id"], "station_name": s["station_name"], "summit": s["site_id"]}
                                 for s in used_stations]},
                "training_period": {"run_init_utc": [runs.min().isoformat(), runs.max().isoformat()],
                                    "run_hours_utc": sorted(set(runs.dt.hour().to_list())),
                                    "forecast_value": "mean of T-1h and T (value_h1); applied to single hourly values"},
                "validated_elevation": validated_elevation(g),
                "provenance": provenance,
                "n": entry["n"], "p_all": entry["p_sunny_all"],
                "bins": [{"label": b["bin"], "n": b["n"], "p_raw": b["p_sunny"], "p": b["p_sunny_used"], "thin": b["thin"]}
                         for b in entry["bins"]],
            })
    doc = {
        "schema": SCHEMA_NAME, "schema_version": 1, "min_mos_version": MOS_VERSION,
        "table_set_id": TABLE_SET_ID, "table_set_version": TABLE_SET_VERSION,
        "description": "R1: summit cloud (%) -> P(sunny), and the effective cloud the plugin v1.5.0 scores with (案C). "
                       "Warm-season daytime only; see each table's time_of_day / season / validated_elevation.",
        "provenance": provenance,
        "binning": {"kind": "zero_atom_right_closed", "edges": BIN_EDGES, "labels": BIN_LABELS},
        "transform": {"kind": "ratio_to_reference", "scale": 100, "clamp": [0, 100], "round_decimals": 1,
                      "reference": {"lead_hours": [0, 48], "bin": 0, "same_model": True},
                      "note": "effective = 100 * (1 - p / p_ref), p_ref = same model's p at 0% for lead 0-48h (案C)"},
        "fallback": {"unknown_model": "ecmwf_ifs025", "lead_out_of_range": "nearest_group"},
        "tables": tables,
    }
    doc["content_sha256"] = content_sha256(doc)
    return doc


def legacy_diffs(table: dict, legacy: dict) -> list[str]:
    out = []
    old = legacy.get("table", {})
    for model, groups in table.items():
        if model not in old or len(old[model]) != len(groups):
            out.append(f"{model}: group count differs")
            continue
        for a, b in zip(groups, old[model]):
            if a["lead_days"] != b["lead_days"] or a["n"] != b["n"]:
                out.append(f"{model} {a['lead_days']}: lead/n differs")
            for x, y in zip(a["bins"], b["bins"]):
                if (x["n"], x["p_sunny"], x["p_sunny_used"]) != (y["n"], y["p_sunny"], y["p_sunny_used"]):
                    out.append(f"{model} {a['lead_days']} {x['bin']}: {x} != {y}")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--allow-legacy-diff", action="store_true")
    args = ap.parse_args(argv)
    sr, obs = pl.read_parquet(SR_PATH), pl.read_parquet(OBS_PATH)
    legacy = json.loads(LEGACY_JSON.read_text(encoding="utf-8")) if LEGACY_JSON.exists() else None
    table, _ = build(sr, obs)
    diffs = legacy_diffs(table, legacy) if legacy else ["legacy JSON not found"]
    if diffs and not args.allow_legacy_diff:
        raise SystemExit("rebuilt table differs from docs/cloud-calibration-table.json:\n  " + "\n  ".join(diffs))
    doc = build_doc(sr, obs, git_info(), date.today(), legacy)
    parse_table_set(json.loads(json.dumps(doc, ensure_ascii=False)))     # 書く前に mos 自身で読めることを確認
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {args.out}  sha256={doc['content_sha256']}  commit={doc['provenance']['commit']} dirty={doc['provenance']['dirty']}"
          f"  legacy_identical={doc['provenance']['legacy_json']['identical']}")
    for t in doc["tables"]:
        ve = t["validated_elevation"]
        print(f"  {t['table_id']:24s} n={t['n']:6d} months={t['season']['months']} confirmed={ve['confirmed_m']} "
              + " ".join(f"{s['site_id']}:{s['days']}d/{s['status'][0]}" for s in ve["sites"]))


if __name__ == "__main__":
    main()
