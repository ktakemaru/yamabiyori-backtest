"""Track B 評価 (Phase 3): 誤差を ① 気圧面 RH 予報の誤差 / ②③ RH→雲量診断+山頂内挿の誤差 に分けて数字を出す。

入力: data/parquet/single_runs_long.parquet (parse_single_runs.py), data/parquet/observation_long.parquet (obs_import.py)

A. ① 富士山 (50066, 3775m) の湿度観測を「気圧面 RH の実測値」として、予報 RH と突き合わせる。
   予報側は 700hPa と 600hPa の RH を **観測した現地気圧 p_obs で ln(p) 内挿** した値 (厳密版) と、
   本体と同じ geopotential_height で 3776m に線形内挿した値 (本体版) の 2 通り。リード時間帯別に MAE/bias/n。
   参考として麓 (白馬・野辺山・奥日光・鷲倉) でも、観測所標高へ内挿した RH と観測湿度を比べる。
B. ②③ 山頂/観測所標高の雲量 (RH→Sundqvist→内挿、本体と同じ) を 2 値化し、日照 sun1h と分割表で比べる。
   - 同じ高さでの検証: 観測所標高に内挿した雲量 vs その観測所の sun1h (②③ だけを測る)
   - 製品と同じ組: 山頂雲量 vs 麓観測所の sun1h (代表性の差も混ざる)
   - 比較対象: 地上 total cloud_cover、cloud_cover_low
   2 値化 (判断・未調整): 観測「晴れ」= sun1h >= 0.4 h、予報「晴れ」= 雲量 <= 50%。昼間 = JST 07..17 時に終わる 1 時間。
   予報値は valid_time の 1 時間 (T-1h と T の平均) を使う (sun1h は T に終わる 1 時間の積算)。

    python -m backtest.trackb_eval [--out docs/track-b-tables.md]
"""
import argparse
import math
from pathlib import Path

import polars as pl

from . import config
from .parse_single_runs import PARQUET_PATH as SR_PATH
from .obs_import import PARQUET_PATH as OBS_PATH

SUN_SUNNY_H = 0.4
CLOUD_CLEAR_PCT = 50.0
DAY_HOURS_JST = list(range(7, 18))          # 07:00〜17:00 JST に終わる 1 時間
LEAD_BUCKETS = [(0, 24), (24, 48), (48, 72), (72, 120), (120, 168), (168, 240)]
SITE_BY_STATION = {s["station_id"]: s for s in config.SITES}


def lead_bucket_expr():
    e = pl.lit(None, dtype=pl.Utf8)
    for lo, hi in reversed(LEAD_BUCKETS):
        e = pl.when((pl.col("lead_hours") >= lo) & (pl.col("lead_hours") < hi)).then(pl.lit(f"{lo:3d}-{hi:3d}h")).otherwise(e)
    return e.alias("lead_bucket")


def hourly_mean_prev(df: pl.DataFrame, value_col="value") -> pl.DataFrame:
    """(site, model, run, variable[, level]) ごとに T-1h と T の平均を value_h1 として付ける。"""
    keys = ["site_id", "model", "run_utc", "variable", "level_hpa"]
    df = df.with_columns(pl.col("level_hpa").fill_null(-1)).sort(keys + ["valid_time"])   # null は join で一致しないので埋める
    prev = df.with_columns((pl.col("valid_time") + pl.duration(hours=1)).alias("valid_time"),
                           pl.col(value_col).alias("value_prev")).select(keys + ["valid_time", "value_prev"])
    out = df.join(prev, on=keys + ["valid_time"], how="left")
    return out.with_columns(((pl.col(value_col) + pl.col("value_prev")) / 2).alias("value_h1"))


def obs_wide(obs: pl.DataFrame, station: str, elements: list[str], prefer="etrn_cache") -> pl.DataFrame:
    """1観測所の要素を横持ちに。同じ時刻に両ソースがあれば prefer を優先。"""
    o = obs.filter((pl.col("station_id") == station) & pl.col("element").is_in(elements) & pl.col("value").is_not_null())
    o = o.with_columns((pl.col("source") == prefer).cast(pl.Int8).alias("pref")).sort("pref", descending=True)
    o = o.unique(subset=["valid_time", "element"], keep="first")
    return o.pivot(on="element", index="valid_time", values="value")


# ---------------------------------------------------------------- A. ① RH
def fuji_rh_table(sr: pl.DataFrame, obs: pl.DataFrame) -> pl.DataFrame:
    site = SITE_BY_STATION["50066"]
    o = obs_wide(obs, "50066", ["humidity", "pressure", "temp"], prefer="amedas_map")
    o = o.filter(pl.col("humidity").is_not_null() & pl.col("pressure").is_not_null())
    f = sr.filter((pl.col("site_id") == site["site_id"]) & (pl.col("variable") == "relative_humidity")
                  & pl.col("level_hpa").is_in([700, 600]))
    f = f.pivot(on="level_hpa", index=["model", "run_utc", "valid_time", "lead_hours"], values="value") \
         .rename({"700": "rh700", "600": "rh600"})
    body = sr.filter((pl.col("site_id") == site["site_id"]) & (pl.col("variable") == "relative_humidity_at_summit")) \
             .select("model", "run_utc", "valid_time", pl.col("value").alias("rh_body"))
    j = f.join(body, on=["model", "run_utc", "valid_time"], how="left").join(o, on="valid_time", how="inner")
    # ln(p) 内挿: w = (ln p_obs - ln 700) / (ln 600 - ln 700)
    j = j.with_columns((((pl.col("pressure").log() - math.log(700.0)) / (math.log(600.0) - math.log(700.0)))).alias("w"))
    j = j.with_columns((pl.col("rh700") + (pl.col("rh600") - pl.col("rh700")) * pl.col("w")).alias("rh_logp"))
    j = j.with_columns(lead_bucket_expr()).filter(pl.col("lead_bucket").is_not_null())
    agg = (j.group_by("model", "lead_bucket")
             .agg((pl.col("rh_logp") - pl.col("humidity")).abs().mean().alias("mae_logp"),
                  (pl.col("rh_logp") - pl.col("humidity")).mean().alias("bias_logp"),
                  (pl.col("rh_body") - pl.col("humidity")).abs().mean().alias("mae_body"),
                  (pl.col("rh_body") - pl.col("humidity")).mean().alias("bias_body"),
                  pl.col("pressure").mean().alias("p_obs_mean"), pl.len().alias("n"))
             .sort("model", "lead_bucket"))
    return agg


def foot_rh_table(sr: pl.DataFrame, obs: pl.DataFrame) -> pl.DataFrame:
    frames = []
    for station, site in SITE_BY_STATION.items():
        if station == "50066":
            continue
        o = obs_wide(obs, station, ["humidity"])
        f = sr.filter((pl.col("site_id") == site["site_id"]) & (pl.col("variable") == "relative_humidity_at_station") & pl.col("level_hpa").is_null())
        j = f.join(o, on="valid_time", how="inner").with_columns(lead_bucket_expr()).filter(pl.col("lead_bucket").is_not_null())
        if j.is_empty():
            continue
        frames.append(j.group_by("model", "lead_bucket").agg(
            (pl.col("value") - pl.col("humidity")).abs().mean().alias("mae"),
            (pl.col("value") - pl.col("humidity")).mean().alias("bias"), pl.len().alias("n"))
            .with_columns(pl.lit(f"{station} {site['station_name']}").alias("station")))
    if not frames:
        return pl.DataFrame()
    return pl.concat(frames).sort("station", "model", "lead_bucket")


# ---------------------------------------------------------------- B. ②③ 雲量 vs 日照
def contingency(df: pl.DataFrame, pred_col: str, group_cols: list[str]) -> pl.DataFrame:
    """obs_sunny (bool), pred_clear (bool) から適中率/空振り率/見逃し率/POD と baseline。"""
    d = df.with_columns((pl.col(pred_col) <= CLOUD_CLEAR_PCT).alias("pred_clear"))
    return (d.group_by(group_cols).agg(
        (pl.col("pred_clear") & pl.col("obs_sunny")).sum().alias("hit"),
        (pl.col("pred_clear") & ~pl.col("obs_sunny")).sum().alias("false_alarm"),
        (~pl.col("pred_clear") & pl.col("obs_sunny")).sum().alias("miss"),
        (~pl.col("pred_clear") & ~pl.col("obs_sunny")).sum().alias("correct_neg"),
        pl.len().alias("n"), pl.col("obs_sunny").mean().alias("obs_sunny_rate"))
        .with_columns(
            ((pl.col("hit") + pl.col("correct_neg")) / pl.col("n")).alias("accuracy"),
            (pl.col("false_alarm") / (pl.col("hit") + pl.col("false_alarm"))).alias("false_alarm_ratio"),
            (pl.col("miss") / (pl.col("hit") + pl.col("miss"))).alias("miss_rate"),
            pl.max_horizontal(pl.col("obs_sunny_rate"), 1 - pl.col("obs_sunny_rate")).alias("baseline_acc"))
        .sort(group_cols))


def cloud_vs_sun(sr: pl.DataFrame, obs: pl.DataFrame, predictor: str) -> pl.DataFrame:
    frames = []
    for station, site in SITE_BY_STATION.items():
        o = obs_wide(obs, station, ["sun1h"])
        if o.is_empty():
            continue
        f = sr.filter((pl.col("site_id") == site["site_id"]) & (pl.col("variable") == predictor) & pl.col("level_hpa").is_null())
        f = hourly_mean_prev(f)
        j = f.join(o, on="valid_time", how="inner")
        j = j.with_columns(pl.col("valid_time").dt.convert_time_zone("Asia/Tokyo").dt.hour().alias("hour_jst"))
        j = j.filter(pl.col("hour_jst").is_in(DAY_HOURS_JST) & pl.col("value_h1").is_not_null())
        j = j.with_columns((pl.col("sun1h") >= SUN_SUNNY_H).alias("obs_sunny"), lead_bucket_expr()).filter(pl.col("lead_bucket").is_not_null())
        if j.is_empty():
            continue
        frames.append(contingency(j, "value_h1", ["model", "lead_bucket"]).with_columns(
            pl.lit(f"{site['site_id']}/{station}").alias("pair"), pl.lit(predictor).alias("predictor")))
    if not frames:
        return pl.DataFrame()
    return pl.concat(frames).sort("predictor", "pair", "model", "lead_bucket")


def threshold_sweep(sr: pl.DataFrame, obs: pl.DataFrame, predictors: list[str], max_lead_h: int = 72,
                    thresholds=(5, 10, 20, 30, 50, 70)) -> pl.DataFrame:
    """2値化の閾値を変えたときの分割表 (全地点プール, lead < max_lead_h)。係数調整ではなく
    「予報雲量のスケールが観測とどれだけズレているか」を測るための診断。"""
    frames = []
    for predictor in predictors:
        parts = []
        for station, site in SITE_BY_STATION.items():
            o = obs_wide(obs, station, ["sun1h"])
            if o.is_empty():
                continue
            f = sr.filter((pl.col("site_id") == site["site_id"]) & (pl.col("variable") == predictor)
                          & pl.col("level_hpa").is_null() & (pl.col("lead_hours") < max_lead_h))
            j = hourly_mean_prev(f).join(o, on="valid_time", how="inner")
            j = j.with_columns(pl.col("valid_time").dt.convert_time_zone("Asia/Tokyo").dt.hour().alias("hour_jst"))
            j = j.filter(pl.col("hour_jst").is_in(DAY_HOURS_JST) & pl.col("value_h1").is_not_null())
            parts.append(j.with_columns((pl.col("sun1h") >= SUN_SUNNY_H).alias("obs_sunny")))
        if not parts:
            continue
        j = pl.concat(parts)
        for th in thresholds:
            d = j.with_columns((pl.col("value_h1") <= th).alias("pred_clear"))
            g = d.group_by("model").agg(
                (pl.col("pred_clear") & pl.col("obs_sunny")).sum().alias("hit"),
                (pl.col("pred_clear") & ~pl.col("obs_sunny")).sum().alias("false_alarm"),
                (~pl.col("pred_clear") & pl.col("obs_sunny")).sum().alias("miss"),
                (~pl.col("pred_clear") & ~pl.col("obs_sunny")).sum().alias("correct_neg"),
                pl.len().alias("n"), pl.col("obs_sunny").mean().alias("obs_sunny_rate"), pl.col("pred_clear").mean().alias("pred_clear_rate"))
            frames.append(g.with_columns(pl.lit(predictor).alias("predictor"), pl.lit(th).alias("threshold")))
    if not frames:
        return pl.DataFrame()
    out = pl.concat(frames)
    return out.with_columns(
        ((pl.col("hit") + pl.col("correct_neg")) / pl.col("n")).alias("accuracy"),
        (pl.col("false_alarm") / (pl.col("hit") + pl.col("false_alarm"))).alias("false_alarm_ratio"),
        (pl.col("miss") / (pl.col("hit") + pl.col("miss"))).alias("miss_rate"),
    ).with_columns(pl.max_horizontal(pl.col("obs_sunny_rate"), 1 - pl.col("obs_sunny_rate")).alias("baseline_acc"))      .select("predictor", "model", "threshold", "n", "pred_clear_rate", "obs_sunny_rate", "accuracy", "baseline_acc", "false_alarm_ratio", "miss_rate")      .sort("predictor", "model", "threshold")


def pooled(df: pl.DataFrame) -> pl.DataFrame:
    if df.is_empty():
        return df
    g = df.group_by("predictor", "model", "lead_bucket").agg(pl.col("hit", "false_alarm", "miss", "correct_neg", "n").sum())
    return g.with_columns(
        ((pl.col("hit") + pl.col("correct_neg")) / pl.col("n")).alias("accuracy"),
        (pl.col("false_alarm") / (pl.col("hit") + pl.col("false_alarm"))).alias("false_alarm_ratio"),
        (pl.col("miss") / (pl.col("hit") + pl.col("miss"))).alias("miss_rate"),
        ((pl.col("hit") + pl.col("miss")) / pl.col("n")).alias("obs_sunny_rate"),
    ).with_columns(pl.max_horizontal(pl.col("obs_sunny_rate"), 1 - pl.col("obs_sunny_rate")).alias("baseline_acc")) \
     .sort("predictor", "model", "lead_bucket")


def fmt(df: pl.DataFrame, floats=3) -> str:
    if df.is_empty():
        return "(no data)"
    pl.Config.set_tbl_rows(200)
    pl.Config.set_tbl_cols(30)
    pl.Config.set_float_precision(floats)
    return str(df)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--sr", type=Path, default=SR_PATH)
    ap.add_argument("--obs", type=Path, default=OBS_PATH)
    ap.add_argument("--out", type=Path, default=None, help="表を markdown 風テキストで書き出す先")
    args = ap.parse_args(argv)
    sr = pl.read_parquet(args.sr)
    obs = pl.read_parquet(args.obs)
    sections = []
    runs = sr.group_by("model").agg(pl.col("run_utc").n_unique().alias("runs"), pl.col("run_utc").min().alias("first"), pl.col("run_utc").max().alias("last")).sort("model")
    sections.append(("入力", f"single_runs_long: {sr.height:,} rows\n{fmt(runs)}\nobservation_long: {obs.height:,} rows"))
    sections.append(("A-1 ① 富士山: 予報 RH (700/600hPa を観測気圧で ln(p) 内挿 = logp、本体と同じ高さ内挿 = body) vs 観測湿度 [%]",
                     fmt(fuji_rh_table(sr, obs), 1)))
    sections.append(("A-2 参考: 麓観測所の標高に内挿した予報 RH vs 観測湿度 [%]", fmt(foot_rh_table(sr, obs), 1)))
    per_pair = []
    for predictor in ["cloud_cover_at_station", "cloud_cover_at_summit", "cloud_cover", "cloud_cover_low"]:
        t = cloud_vs_sun(sr, obs, predictor)
        per_pair.append(t)
        sections.append((f"B ②③ 分割表 (predictor={predictor}) 地点別", fmt(t.select("predictor", "pair", "model", "lead_bucket", "n", "accuracy", "baseline_acc", "false_alarm_ratio", "miss_rate", "obs_sunny_rate") if not t.is_empty() else t)))
    allpairs = pl.concat([t for t in per_pair if not t.is_empty()]) if any(not t.is_empty() for t in per_pair) else pl.DataFrame()
    sections.append(("B プール (全地点) predictor × model × lead", fmt(pooled(allpairs).select("predictor", "model", "lead_bucket", "n", "accuracy", "baseline_acc", "false_alarm_ratio", "miss_rate", "obs_sunny_rate") if not allpairs.is_empty() else allpairs)))
    sections.append(("B 閾値スイープ (lead < 72h, 全地点プール): 予報雲量のスケール診断 (係数調整ではない)",
                     fmt(threshold_sweep(sr, obs, ["cloud_cover_at_station", "cloud_cover_at_summit", "cloud_cover", "cloud_cover_low"]))))
    text = "\n\n".join(f"## {title}\n{body}" for title, body in sections)
    text += chr(10) * 2 + main_discrimination(args.sr, args.obs)
    print(text)
    if args.out:
        args.out.write_text(text, encoding="utf-8")




# ---------------------------------------------------------------- C. 閾値に依存しない識別性能 (AUC / PSS)
from . import skill  # noqa: E402


def discrimination_pairs(sr: pl.DataFrame, obs: pl.DataFrame, predictor: str) -> pl.DataFrame:
    """(pair, model, lead_bucket, valid_time, score=-cloud, obs_sunny) の行を全地点分作る。"""
    parts = []
    for station, site in SITE_BY_STATION.items():
        o = obs_wide(obs, station, ["sun1h"])
        if o.is_empty():
            continue
        f = sr.filter((pl.col("site_id") == site["site_id"]) & (pl.col("variable") == predictor) & pl.col("level_hpa").is_null())
        if f.is_empty():
            continue
        j = hourly_mean_prev(f).join(o, on="valid_time", how="inner")
        j = j.with_columns(pl.col("valid_time").dt.convert_time_zone("Asia/Tokyo").dt.hour().alias("hour_jst"))
        j = j.filter(pl.col("hour_jst").is_in(DAY_HOURS_JST) & pl.col("value_h1").is_not_null())
        j = j.with_columns((pl.col("sun1h") >= SUN_SUNNY_H).alias("obs_sunny"), lead_bucket_expr(),
                           pl.lit(f"{site['site_id']}/{station}").alias("pair"), pl.lit(predictor).alias("predictor"))
        parts.append(j.filter(pl.col("lead_bucket").is_not_null()).select("predictor", "pair", "model", "lead_bucket", "valid_time", "value_h1", "obs_sunny"))
    return pl.concat(parts) if parts else pl.DataFrame()


def discrimination_table(pairs_df: pl.DataFrame, by_pair: bool) -> pl.DataFrame:
    """AUC (晴れが高スコア = −雲量), DeLong SE, 95%CI 下限, PSS@50%, PSS_max とその閾値。標本数併記。"""
    if pairs_df.is_empty():
        return pl.DataFrame()
    keys = ["predictor", "model", "lead_bucket"] + (["pair"] if by_pair else [])
    rows = []
    for key, g in pairs_df.group_by(keys, maintain_order=True):
        cloud = g["value_h1"].to_list()
        lab = g["obs_sunny"].to_list()
        scores = [-c for c in cloud]
        a, se, n1, n0 = skill.auc(scores, lab)
        p50, pod, pofd, *_ = skill.pss([c <= CLOUD_CLEAR_PCT for c in cloud], lab)
        pmax, th = skill.pss_max(scores, lab)
        rows.append({**dict(zip(keys, key)), "n": len(lab), "n_sunny": n1, "obs_sunny_rate": n1 / len(lab),
                     "auc": a, "auc_se": se, "auc_ci95_lo": (a - 1.96 * se) if a is not None else None,
                     "pss_at_50": p50, "pod_at_50": pod, "pofd_at_50": pofd,
                     "pss_max": pmax, "cloud_th_at_pss_max": (-th) if th is not None else None})
    return pl.DataFrame(rows).sort(keys)


def discrimination_report(sr: pl.DataFrame, obs: pl.DataFrame,
                          predictors=("cloud_cover_at_station", "cloud_cover_at_summit", "cloud_cover")) -> dict:
    allp = pl.concat([d for d in (discrimination_pairs(sr, obs, p) for p in predictors) if not d.is_empty()])
    return {"pooled": discrimination_table(allp, by_pair=False), "by_pair": discrimination_table(allp, by_pair=True)}


def main_discrimination(sr_path: Path = SR_PATH, obs_path: Path = OBS_PATH, out: Path = None) -> str:
    sr = pl.read_parquet(sr_path)
    obs = pl.read_parquet(obs_path)
    rep = discrimination_report(sr, obs)
    cols = ["predictor", "model", "lead_bucket", "n", "n_sunny", "obs_sunny_rate", "auc", "auc_se", "auc_ci95_lo", "pss_at_50", "pss_max", "cloud_th_at_pss_max"]
    text = ("## C-1 識別性能 (全地点プール): AUC (雲量を符号反転したスコアで晴れを識別), PSS@50%, PSS_max\n"
            + fmt(rep["pooled"].select(cols) if not rep["pooled"].is_empty() else rep["pooled"])
            + "\n\n## C-2 同上 地点別\n"
            + fmt(rep["by_pair"].select(["pair"] + cols) if not rep["by_pair"].is_empty() else rep["by_pair"]))
    if out:
        out.write_text(text, encoding="utf-8")
    return text


if __name__ == "__main__":
    main()
