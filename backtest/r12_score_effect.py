"""R12 の候補表でスコアがどう動くか: 本体 v1.5.0 と同じ計算で、今の本体 (旧表)・移植表・新表の日スコアを比べる。本体は変更しない。

方法は R1 のとき (本体 scratch_r1_calibration_effect.py / scratch_validate_refs.py @52d3d2a) と同じ:
- 探索キャッシュ: 2026-09-18 20 時取得の 76 座 × 15 日 (R1 の影響確認と同じもの。C:/mountain-weather-r8/cache_prev/ に残っている。読むだけ)
- 参照日: 唐松岳 9/6・立山 9/5・槍ヶ岳 9/5 (本体 cache/ の *_1d_past5d.json。読むだけ; today = その日)
- MSM 優先の結合 → add_altitude_columns → climb_layer_moist_series → 較正した山頂雲量 → detail.compute_day_scores。
本体のコード (core / detail / terrain) は 52d3d2a を git show で一時フォルダに取り出して import する (本体のファイル・.git には書かない)。
表の差し替えはバックテスト側で行う: 較正列 (SUMMIT_CLOUD_CAL_VAR) を「本体の糊 (MSM 降水が非 null なら jma_msm、暦日差の lead) + mos」で作る。
旧表では、この列が本体の calibrated_summit_cloud_series と完全に一致することを毎回確かめる。

    python -m backtest.r12_score_effect [--cache C:/mountain-weather-r8/cache_prev] [--out docs/r12-score-effect.txt]
    python -m backtest.r12_score_effect --body-commit <v1.6.0 の hash> [--out docs/r12-plugin-effect-2026-09-18-cache.txt]

--body-commit: 表の差し替えではなく、v1.5.0 (52d3d2a) と指定コミットの本体コードそのもので日スコアを計算し、
山日ごとに「v1.5.0 = 旧表の計算」「指定コミット = 新表 (msm7) の計算」が完全に一致することを確かめてから、前後の変化を出す。
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


def load_body(tmp: Path, commit: str = BODY_COMMIT):
    """本体モジュールを tmp に取り出して import (sys.dont_write_bytecode)。既に import 済みの本体モジュールは外してから読む。"""
    def git(*a):
        return subprocess.run(["git", "--no-optional-locks", "-C", str(BODY_ROOT), *a], capture_output=True, check=True).stdout
    for f, blob in BODY_FILES.items():
        if blob and commit == BODY_COMMIT:
            assert git("rev-parse", f"{commit}:{f}").decode().strip() == blob, f
        (tmp / f).write_bytes(git("show", f"{commit}:{f}"))
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


def body_native_scores(commit: str, cache: Path) -> tuple[dict, dict]:
    """指定コミットの本体コードそのもので (較正列も本体の calibrated_summit_cloud_series)、山日スコアと参照日スコアを計算する。"""
    tmp = Path(tempfile.mkdtemp(prefix=f"body_{commit[:7]}_"))
    try:
        core, detail = load_body(tmp, commit)
        days, refs = {}, {}
        for mtn in core.MOUNTAINS:
            key = f"{mtn['lat']:.4f}_{mtn['lon']:.4f}_15d.json"
            try:
                msm, ec = read_json(cache / f"jma_msm_{key}"), read_json(cache / f"ecmwf_ifs025_{key}")
            except FileNotFoundError:
                continue
            hourly, sun, summit_m, wind_var, temp_var = build_hourly(core, detail, msm, ec, mtn["elevation_m"])
            hourly[core.SUMMIT_CLOUD_CAL_VAR] = core.calibrated_summit_cloud_series(hourly, summit_m)
            for day, r in detail.compute_day_scores({"hourly": hourly, **sun}, wind_var, summit_m, temp_var).items():
                days[(mtn["name"], day)] = r["score"]
        for name, day in REFS:
            mtn = next(x for x in core.MOUNTAINS if x["name"] == name)
            key = f"{mtn['lat']:.4f}_{mtn['lon']:.4f}_1d_past5d.json"
            msm, ec = read_json(REF_CACHE / f"jma_msm_{key}"), read_json(REF_CACHE / f"ecmwf_ifs025_{key}")
            hourly, sun, summit_m, wind_var, temp_var = build_hourly(core, detail, msm, ec, mtn["elevation_m"])
            hourly[core.SUMMIT_CLOUD_CAL_VAR] = core.calibrated_summit_cloud_series(hourly, summit_m, today=date.fromisoformat(day))
            refs[f"{name} {day}"] = detail.compute_day_scores({"hourly": hourly, **sun}, wind_var, summit_m, temp_var)[day]["score"]
        return days, refs
    finally:
        sys.path.remove(str(tmp))
        for name in ("mountain_weather_core", "mountain_terrain", "mountain_weather_detail"):
            sys.modules.pop(name, None)
        shutil.rmtree(tmp, ignore_errors=True)


DELTA_BUCKETS = ["<-20", "-20..-10", "-10..-5", "-5..0", "0", "0..+5", "+5..+10", ">+10"]


def bucket(d: float) -> str:
    """R1 の記録 (r1-plugin-effect-2026-09-18-cache.txt) と同じ区分。0 は変化なし。"""
    if d == 0:
        return "0"
    return ("<-20" if d < -20 else "-20..-10" if d < -10 else "-10..-5" if d < -5 else "-5..0" if d < 0 else
            "0..+5" if d <= 5 else "+5..+10" if d <= 10 else ">+10")


def spearman(a: list, b: list) -> float:
    """順位相関 (同順位は平均順位)。"""
    def ranks(x):
        order = sorted(range(len(x)), key=lambda i: x[i])
        r = [0.0] * len(x)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and x[order[j + 1]] == x[order[i]]:
                j += 1
            for k in range(i, j + 1):
                r[order[k]] = (i + j) / 2 + 1
            i = j + 1
        return r
    ra, rb = ranks(a), ranks(b)
    ma, mb = sum(ra) / len(ra), sum(rb) / len(rb)
    cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    va = sum((x - ma) ** 2 for x in ra) ** 0.5
    vb = sum((y - mb) ** 2 for y in rb) ** 0.5
    return cov / (va * vb) if va and vb else 1.0


def ranking_table(d: pl.DataFrame, a: str, b: str, top_n: int = 10) -> pl.DataFrame:
    """日ごと: 上位 top_n 座の入れ替わり数、全山の順位の Spearman 相関 (前後)、平均スコアの前後差。"""
    rows = []
    for (day,), g in d.group_by("day", maintain_order=True):
        g = g.sort("name")
        before = g.sort(b, descending=True)["name"].to_list()[:top_n]
        after = g.sort(a, descending=True)["name"].to_list()[:top_n]
        rows.append({"day": day, "lead": g["lead"][0], "n": g.height, f"top{top_n}_replaced": len(set(before) - set(after)),
                     "spearman": spearman(g[b].to_list(), g[a].to_list()), "mean_before": g[b].mean(), "mean_after": g[a].mean(),
                     "mean_diff": g[a].mean() - g[b].mean()})
    return pl.DataFrame(rows).sort("day")


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
    ap.add_argument("--body-commit", help="v1.6.0 など、v1.5.0 と比べる本体のコミット")
    args = ap.parse_args(argv)
    if args.body_commit:
        return main_body_commit(args)
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


def main_body_commit(args) -> int:
    """v1.5.0 と指定コミットの本体コードそのもので前後比較する。表の差し替え (mos) での計算と山日ごとに完全一致することを先に確かめる。"""
    pl.Config.set_tbl_width_chars(260)
    tsets = {k: mos.parse_table_set(read_json(TABLES[k])) for k in ("old", "new")}
    old_days, old_refs = body_native_scores(BODY_COMMIT, args.cache)
    new_days, new_refs = body_native_scores(args.body_commit, args.cache)
    # 表の差し替え (mos) での計算
    tmp = Path(tempfile.mkdtemp(prefix="body_v150_glue_"))
    glue_old, glue_new, meta = {}, {}, {}
    try:
        core, detail = load_body(tmp)
        for mtn in core.MOUNTAINS:
            key = f"{mtn['lat']:.4f}_{mtn['lon']:.4f}_15d.json"
            try:
                msm, ec = read_json(args.cache / f"jma_msm_{key}"), read_json(args.cache / f"ecmwf_ifs025_{key}")
            except FileNotFoundError:
                continue
            s = scores(core, detail, tsets, msm, ec, mtn["elevation_m"])
            first = date.fromisoformat(msm["hourly"]["time"][0][:10])
            for day in s["old"]:
                glue_old[(mtn["name"], day)] = s["old"][day]["score"]
                glue_new[(mtn["name"], day)] = s["new"][day]["score"]
                meta[(mtn["name"], day)] = (mtn["elevation_m"], (date.fromisoformat(day) - first).days + 1)
    finally:
        sys.path.remove(str(tmp))
        for name in ("mountain_weather_core", "mountain_terrain", "mountain_weather_detail"):
            sys.modules.pop(name, None)
        shutil.rmtree(tmp, ignore_errors=True)
    keys = sorted(old_days)
    assert keys == sorted(new_days) == sorted(glue_old)
    mism_old = [k for k in keys if old_days[k] != glue_old[k]]
    mism_new = [k for k in keys if new_days[k] != glue_new[k]]
    rows = []
    for k in keys:
        if old_days[k] is None:
            continue
        elev, lead_day = meta[k]
        rows.append({"name": k[0], "day": k[1], "elevation_m": elev, "band": band_of(elev),
                     "lead": "d1-2" if lead_day <= 2 else "d3+", "old": old_days[k], "new": new_days[k]})
    d = pl.DataFrame(rows)
    buckets = (d.filter(~(((pl.col("old") == 0) & (pl.col("new") > 0)) | ((pl.col("old") > 0) & (pl.col("new") == 0))))
               .with_columns((pl.col("new") - pl.col("old")).map_elements(bucket, return_dtype=pl.String).alias("bucket"))
               .group_by("lead", "bucket").agg(pl.len().alias("n")).sort("lead", "bucket"))
    expected_refs = {"唐松岳 2026-09-06": 55.0, "立山(雄山) 2026-09-05": 95.6, "槍ヶ岳 2026-09-05": 67.8}
    ref_rows = [{"ref": r, "v1.5.0": old_refs[r], "body_commit": new_refs[r], "expected": expected_refs[r],
                 "match": new_refs[r] == expected_refs[r]} for r in expected_refs]
    parts = [f"## 本体 {args.body_commit[:7]} と v1.5.0 (52d3d2a) の前後比較 (本体のコードそのもので計算)",
             f"探索キャッシュ: {args.cache} (2026-09-18 20 時取得、76 座 × 15 日、読むだけ)。山日 {d.height}。",
             "確認 1: v1.5.0 のスコア == 旧表 (mos, r1-summit-cloud-sunny) で差し替えた計算、山日ごとに完全一致: "
             + ("OK (不一致 0)" if not mism_old else f"NG 不一致 {len(mism_old)}: {mism_old[:5]}"),
             f"確認 2: {args.body_commit[:7]} のスコア == 新表 (mos, r1-summit-cloud-sunny-msm7) で差し替えた計算、山日ごとに完全一致: "
             + ("OK (不一致 0)" if not mism_new else f"NG 不一致 {len(mism_new)}: {mism_new[:5]}"),
             "→ 両方 OK なら、下の変化は r12-score-effect.txt の「新表 − 今の本体」と山日ごとに同じ。", "",
             "### 変化量 (本体の新コミット − v1.5.0)、0 点をまたぐ変化は別枠", fmt(summarize(d, "new", "old", ["lead"]), 2),
             "標高帯別", fmt(summarize(d, "new", "old", ["lead", "band"]), 2),
             "### 変化量の区分 (R1 の記録と同じ区分、0 点をまたぐ変化を除く)", fmt(buckets, 0),
             "### 参照日", fmt(pl.DataFrame(ref_rows), 1),
             "### 探索モードの順位 (日ごと: 上位 10 座の入れ替わり、全 76 座の順位の Spearman 相関、平均スコアの前後差)",
             "1〜2 日目だけが動くので、1〜2 日目の平均スコアが 3 日目以降と比べて一律に上下していないかを mean_diff で見る。",
             fmt(ranking_table(d, "new", "old"), 3)]
    txt = "\n".join(parts) + "\n"
    args.out.write_text(txt, encoding="utf-8")
    print(txt)
    return 0 if not mism_old and not mism_new and all(r["match"] for r in ref_rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
