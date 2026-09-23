"""R12 の候補表でスコアがどう動くか: 本体 v1.5.0 と同じ計算で、今の本体 (旧表)・移植表・新表の日スコアを比べる。本体は変更しない。

方法は R1 のとき (本体 scratch_r1_calibration_effect.py / scratch_validate_refs.py @52d3d2a) と同じ:
- 探索キャッシュ: 2026-09-18 20 時取得の 76 座 × 15 日 (R1 の影響確認と同じもの。C:/mountain-weather-r8/cache_prev/ に残っている。読むだけ)
- 参照日: 唐松岳 9/6・立山 9/5・槍ヶ岳 9/5 (本体 cache/ の *_1d_past5d.json。読むだけ; today = その日)
- MSM 優先の結合 → add_altitude_columns → climb_layer_moist_series → 較正した山頂雲量 → detail.compute_day_scores。
本体のコード (core / detail / terrain) は 52d3d2a を git show で一時フォルダに取り出して import する (本体のファイル・.git には書かない)。
表の差し替えはバックテスト側で行う: 較正列 (SUMMIT_CLOUD_CAL_VAR) を「本体の糊 (MSM 降水が非 null なら jma_msm、暦日差の lead) + mos」で作る。
旧表では、この列が本体の calibrated_summit_cloud_series と完全に一致することを毎回確かめる。

    python -m backtest.r12_score_effect [--cache C:/mountain-weather-r8/cache_prev] [--out docs/r12-score-effect.txt]
"""
import argparse
import importlib
import json
import shutil
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

import polars as pl

import mos

from . import config
from .trackb_eval import fmt

REPO = config.DATA_DIR.parent
BODY_ROOT = Path("C:/mountain-weather")
BODY_COMMIT = "52d3d2a5560ec35329de1b47f6a836b658db15d6"
BODY_FILES = {"mountain_weather_core.py": "1aaf5d83668ffbd77bc91640fc9a15960696c868", "mountain_weather_detail.py": None,
              "mountain_terrain.py": None}
DEFAULT_CACHE = Path("C:/mountain-weather-r8/cache_prev")
REF_CACHE = BODY_ROOT / "cache"
REFS = [("唐松岳", "2026-09-06"), ("立山(雄山)", "2026-09-05"), ("槍ヶ岳", "2026-09-05")]
TABLES = {"old": REPO / "mos_tables" / "r1-summit-cloud-sunny.json",
          "transplant": REPO / "mos_tables" / "r1-summit-cloud-sunny-msm7-transplant.json",
          "new": REPO / "mos_tables" / "r1-summit-cloud-sunny-msm7.json"}
OUT = REPO / "docs" / "r12-score-effect.txt"
KW = {"predictor": "cloud_cover_at_summit", "target": "p_sunny"}
BANDS = [("<1500", 0, 1499), ("1500-2999 (R12 大)", 1500, 2999), (">=3000", 3000, 9999)]


def load_body(tmp: Path):
    """52d3d2a の本体モジュールを tmp に取り出して import (sys.dont_write_bytecode)。"""
    def git(*a):
        return subprocess.run(["git", "--no-optional-locks", "-C", str(BODY_ROOT), *a], capture_output=True, check=True).stdout
    for f, blob in BODY_FILES.items():
        if blob:
            assert git("rev-parse", f"{BODY_COMMIT}:{f}").decode().strip() == blob, f
        (tmp / f).write_bytes(git("show", f"{BODY_COMMIT}:{f}"))
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(tmp))
    for name in ("mountain_weather_core", "mountain_terrain", "mountain_weather_detail"):
        sys.modules.pop(name, None)
    core = importlib.import_module("mountain_weather_core")
    detail = importlib.import_module("mountain_weather_detail")
    assert Path(core.__file__).parent == tmp and Path(detail.__file__).parent == tmp
    return core, detail


def read_json(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def build_hourly(core, detail, msm: dict, ec: dict, elevation_m: float):
    summit_m, wind_var, _ = detail.wind_vars_for_elevation(elevation_m)
    temp_var = detail.temp_var_for_elevation(elevation_m)
    ec_idx = {t: i for i, t in enumerate(ec["hourly"]["time"])}
    hourly = {"time": msm["hourly"]["time"]}
    for var in set(msm["hourly"]) | set(ec["hourly"]):
        if var == "time":
            continue
        mv, fv = msm["hourly"].get(var, []), ec["hourly"].get(var, [])
        merged = []
        for i, t in enumerate(hourly["time"]):
            v = mv[i] if i < len(mv) else None
            if v is None:
                j = ec_idx.get(t)
                v = fv[j] if j is not None and j < len(fv) else None
            merged.append(v)
        hourly[var] = merged
    hourly[core.MSM_PRECIP_VAR] = list(msm["hourly"]["precipitation"])
    targets = {core.band_label(a): a for a in core.FIXED_ALTITUDE_BANDS_M}
    targets[core.SUMMIT_LABEL] = summit_m
    core.add_altitude_columns(hourly, targets)
    hourly[core.CLIMB_LAYER_MOIST_VAR] = core.climb_layer_moist_series(hourly, summit_m)
    sun = {"_daily_sunrise": dict(zip(ec["daily"]["time"], ec["daily"]["sunrise"])),
           "_daily_sunset": dict(zip(ec["daily"]["time"], ec["daily"]["sunset"]))}
    return hourly, sun, summit_m, wind_var, temp_var


def glue(core, ts, hourly: dict, summit_m: float, today: date = None) -> list:
    """本体 calibrated_summit_cloud_series と同じ糊 (tests/test_mos_equivalence.body_glue と同じ) で、表だけ差し替える。"""
    w = core.cloud_calibration_weight(summit_m)
    raw = hourly[core.SUMMIT_VARS["cloudcover"]]
    times = hourly["time"]
    today = today or date.fromisoformat(times[0][:10])
    msm = hourly.get(core.MSM_PRECIP_VAR) or [None] * len(times)
    out = []
    for i, t in enumerate(times):
        model = "jma_msm" if (i < len(msm) and msm[i] is not None) else "ecmwf_ifs025"
        lead_day = max(1, (date.fromisoformat(t[:10]) - today).days + 1)
        v = mos.calibrate(ts, raw[i], model=model, lead_hours=24 * (lead_day - 1), hour_jst=int(t[11:13]), month=int(t[5:7]),
                          on_mismatch="allow", **KW).value
        out.append(None if v is None else round(w * v + (1.0 - w) * raw[i], 1))
    return out


def scores(core, detail, tsets: dict, msm: dict, ec: dict, elevation_m: float, today: date = None) -> dict:
    hourly, sun, summit_m, wind_var, temp_var = build_hourly(core, detail, msm, ec, elevation_m)
    body_series = core.calibrated_summit_cloud_series(hourly, summit_m, today=today)
    out = {}
    for name, ts in tsets.items():
        series = glue(core, ts, hourly, summit_m, today)
        if name == "old":
            assert series == body_series, "旧表の較正列が本体と一致しない"
        h = dict(hourly)
        h[core.SUMMIT_CLOUD_CAL_VAR] = series
        out[name] = detail.compute_day_scores({"hourly": h, **sun}, wind_var, summit_m, temp_var)
    return out


def band_of(elev: int) -> str:
    return next(n for n, lo, hi in BANDS if lo <= elev <= hi)


def summarize(d: pl.DataFrame, a: str, b: str, keys: list) -> pl.DataFrame:
    """a − b の変化量。0 点から動いた組 (b = 0 → a > 0) と 0 点へ落ちた組 (b > 0 → a = 0) は別枠で数え、分布からは除く。"""
    dd = pl.col(a) - pl.col(b)
    revived = (pl.col(b) == 0) & (pl.col(a) > 0)
    zeroed = (pl.col(b) > 0) & (pl.col(a) == 0)
    core_rows = ~revived & ~zeroed
    return (d.group_by(keys).agg(
        pl.len().alias("n_mountain_days"),
        (dd != 0).sum().alias("n_changed"),
        dd.filter(core_rows).mean().alias("mean_d"),
        dd.filter(core_rows).abs().mean().alias("mean_abs_d"),
        (dd.filter(core_rows).abs() >= 5).mean().alias("share_abs_ge5"),
        dd.filter(core_rows).min().alias("min_d"), dd.filter(core_rows).max().alias("max_d"),
        revived.sum().alias("revived_from_0"), zeroed.sum().alias("dropped_to_0"))
        .sort(keys))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args(argv)
    pl.Config.set_tbl_width_chars(260)
    tsets = {k: mos.parse_table_set(read_json(p)) for k, p in TABLES.items()}
    tmp = Path(tempfile.mkdtemp(prefix="body_v150_"))
    try:
        core, detail = load_body(tmp)
        rows, missing, fetched = [], [], set()
        for mtn in core.MOUNTAINS:
            key = f"{mtn['lat']:.4f}_{mtn['lon']:.4f}_15d.json"
            try:
                msm, ec = read_json(args.cache / f"jma_msm_{key}"), read_json(args.cache / f"ecmwf_ifs025_{key}")
            except FileNotFoundError:
                missing.append(mtn["name"])
                continue
            fetched.add(msm.get("_fetched_at"))
            s = scores(core, detail, tsets, msm, ec, mtn["elevation_m"])
            first = date.fromisoformat(msm["hourly"]["time"][0][:10])
            for day in sorted(s["old"]):
                if s["old"][day]["score"] is None:
                    continue
                rows.append({"name": mtn["name"], "elevation_m": mtn["elevation_m"], "band": band_of(mtn["elevation_m"]), "day": day,
                             "lead_day": (date.fromisoformat(day) - first).days + 1,
                             "old": s["old"][day]["score"], "transplant": s["transplant"][day]["score"], "new": s["new"][day]["score"],
                             "cloud_old": s["old"][day]["cloud_pct"], "cloud_tp": s["transplant"][day]["cloud_pct"],
                             "cloud_new": s["new"][day]["cloud_pct"]})
        refs = []
        for name, day in REFS:
            mtn = next(x for x in core.MOUNTAINS if x["name"] == name)
            key = f"{mtn['lat']:.4f}_{mtn['lon']:.4f}_1d_past5d.json"
            msm, ec = read_json(REF_CACHE / f"jma_msm_{key}"), read_json(REF_CACHE / f"ecmwf_ifs025_{key}")
            s = scores(core, detail, tsets, msm, ec, mtn["elevation_m"], today=date.fromisoformat(day))
            refs.append({"ref": f"{name} {day}", "elevation_m": mtn["elevation_m"],
                         **{k: s[k][day]["score"] for k in TABLES}, **{f"cloud_{k}": s[k][day]["cloud_pct"] for k in TABLES}})
    finally:
        sys.path.remove(str(tmp))
        shutil.rmtree(tmp, ignore_errors=True)
    d = pl.DataFrame(rows).with_columns(pl.when(pl.col("lead_day") <= 2).then(pl.lit("d1-2")).otherwise(pl.lit("d3+")).alias("lead"))
    pairs = [("移植表 − 今の本体", "transplant", "old"), ("新表 − 今の本体", "new", "old"), ("移植表 − 新表", "transplant", "new")]
    parts = ["## R12 候補表によるスコアの変化 (本体 v1.5.0 の計算で、較正表だけ差し替え)",
             f"探索キャッシュ: {args.cache} (_fetched_at {sorted(x for x in fetched if x)[:1]}; 76 座中 {76 - len(missing)} 座、欠け {missing})、"
             f"山日 {d.height}。旧表の較正列は全山で本体の calibrated_summit_cloud_series と一致を確認済み。",
             "変化量の統計 (mean_d / mean_abs_d / share_abs_ge5 / min / max) は 0 点から動いた組・0 点へ落ちた組を除く。それらは revived_from_0 / dropped_to_0 に別枠。",
             "表の差は MSM の d1-2 行だけなので、変わるのは MSM の値がある時間を含む日 (取得日から 1〜2 日目、lead=d1-2) が中心。",
             ""]
    for title, a, b in pairs:
        parts += [f"### {title}", "全体", fmt(summarize(d, a, b, ["lead"]), 2), "標高帯別 (1500-2999 = R12 の影響が大きい帯)",
                  fmt(summarize(d, a, b, ["lead", "band"]), 2), ""]
    parts += ["### 参照日 (today = その日、lead 1)", fmt(pl.DataFrame(refs), 1), "",
              "### 変化の大きい山日 (新表 − 今の本体、d1-2)",
              fmt(d.filter(pl.col("lead") == "d1-2").with_columns((pl.col("new") - pl.col("old")).alias("d_new"),
                                                                  (pl.col("transplant") - pl.col("old")).alias("d_tp"))
                  .sort(pl.col("d_new").abs(), descending=True).head(15)
                  .select("name", "elevation_m", "day", "old", "transplant", "new", "d_tp", "d_new", "cloud_old", "cloud_tp", "cloud_new"), 1)]
    txt = "\n".join(parts) + "\n"
    args.out.write_text(txt, encoding="utf-8")
    print(txt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
