"""降水閾値 0.1mm/h の実害を既存データで測る (Track A: Previous Runs の毎時 precipitation vs アメダス 1 時間降水量)。

背景 (api-findings §11.2): ECMWF の毎時 precipitation は「3h 積算 ÷3 を 0.1mm 丸め」なので、本体の
「毎時 ≥0.1mm = 降水あり」(ENSEMBLE_PRECIP_WET_THRESHOLD_MM; 決定論側の WET_HOUR_MSM_PRECIP_MM は MSM (ネイティブ 1h) に対して)
は ECMWF では実質「3h 積算 0.15〜0.3mm」を拾う。ここでは
  1. 本体の基準 (≥0.1mm/h) で「降水あり」となる時間のうち、実況が 0.0mm (アメダスの最小単位 0.5mm 未満) だった割合を
     モデル × lead × 地点で出す
  2. 予報値の分布 (0.1 の丸め境界に張り付いているか; ECMWF は 3h ブロック内で同じ値が続くか)
  3. 閾値候補 (0.1〜2.0mm/h) ごとの分割表 (POD / FAR / PSS / bias) → 候補を提案
実況の「降水あり」は precipitation1h ≥ 0.5mm (アメダスの分解能。0.0 には「無降水」と「0.5mm 未満の微量」の両方が含まれ、区別できない)。
時刻はどちらも「HH:00 に終わる 1 時間」。全時間帯 (夜間含む)。

    python -m backtest.precip_threshold      # docs/precip-threshold-tables.txt
"""
import argparse
from pathlib import Path

import polars as pl

from . import config, skill
from .trackb_eval import SITE_BY_STATION, fmt, obs_wide

FORECAST_LONG = config.PARQUET_PATH
OBS_LONG = config.DATA_DIR / "parquet" / "observation_long.parquet"
OUT_PATH = config.DATA_DIR.parent / "docs" / "precip-threshold-tables.txt"
BODY_THRESHOLD = 0.1
OBS_WET_MM = 0.5
CANDIDATES = [0.1, 0.2, 0.3, 0.4, 0.5, 0.7, 1.0, 1.5, 2.0]
FC_BINS = [(0.1, 0.1), (0.2, 0.2), (0.3, 0.4), (0.5, 0.9), (1.0, 1.9), (2.0, 1e9)]


def pairs(fc: pl.DataFrame, obs: pl.DataFrame) -> pl.DataFrame:
    parts = []
    for station, site in SITE_BY_STATION.items():
        o = obs_wide(obs, station, ["precipitation1h"])
        if o.is_empty():
            continue
        f = fc.filter((pl.col("site_id") == site["site_id"]) & (pl.col("variable") == "precipitation") & pl.col("value").is_not_null())
        j = f.join(o, on="valid_time", how="inner")
        j = j.with_columns((pl.col("precipitation1h") >= OBS_WET_MM).alias("obs_wet"),
                           pl.lit(f"{site['site_id']}/{station}").alias("pair"))
        parts.append(j.select("pair", "model", "lead_days", "valid_time", pl.col("value").alias("fc_mm"), "precipitation1h", "obs_wet"))
    return pl.concat(parts) if parts else pl.DataFrame()


def false_alarm_at_body_threshold(d: pl.DataFrame, keys) -> pl.DataFrame:
    """本体基準で降水ありとした時間のうち実況 0.0mm の割合 + 予報値ビン別。"""
    w = d.filter(pl.col("fc_mm") >= BODY_THRESHOLD)
    rows = []
    for key, g in w.group_by(keys, maintain_order=True):
        r = {**dict(zip(keys, key)), "n_fc_wet": g.height, "share_obs_0.0": float(1 - g["obs_wet"].mean()),
             "share_fc_exactly_0.1": float((g["fc_mm"] == 0.1).mean()),
             "share_fc_0.1_0.2": float((g["fc_mm"] <= 0.2).mean())}
        for lo, hi in FC_BINS:
            b = g.filter((pl.col("fc_mm") >= lo) & (pl.col("fc_mm") <= hi))
            r[f"obs0_fc[{lo},{hi if hi < 1e9 else 'inf'}]"] = (round(1 - b["obs_wet"].mean(), 2) if b.height else None)
            r[f"n_fc[{lo},{hi if hi < 1e9 else 'inf'}]"] = b.height
        rows.append(r)
    return pl.DataFrame(rows).sort(keys)


def block_key():
    """3h ブロックのキー: 毎時値は「T に終わる 1 時間」なので、ネイティブ 3h ステップ (T0+3h に終わる) は T0+1h, +2h, +3h の 3 つ。
    valid_time − 1h を 3h に切り捨てると同じブロックに入る (日付境界もまたぐ)。"""
    return (pl.col("valid_time") - pl.duration(hours=1)).dt.truncate("3h").alias("blk")


def value_distribution(d: pl.DataFrame) -> pl.DataFrame:
    """予報値の分布: 0 / 0.1 / 0.2 / 0.3-0.4 / 0.5-0.9 / >=1.0 の割合 (全時間) と、ECMWF の 3h ブロック内一致率。"""
    rows = []
    for (model, lead), g in d.group_by(["model", "lead_days"], maintain_order=True):
        v = g["fc_mm"]
        r = {"model": model, "lead_days": lead, "n": g.height, "share_0": float((v == 0).mean()), "share_0.1": float((v == 0.1).mean()),
             "share_0.2": float((v == 0.2).mean()), "share_0.3_0.4": float(((v >= 0.3) & (v <= 0.4)).mean()),
             "share_0.5_0.9": float(((v >= 0.5) & (v <= 0.9)).mean()), "share_ge1.0": float((v >= 1.0).mean()),
             "obs_wet_rate": float(g["obs_wet"].mean())}
        # 3h ブロック (UTC 00-03, 03-06, ...) 内で値が全て同じ割合 (正値のブロックのみ)
        gg = g.with_columns(block_key())
        blk = gg.group_by(["pair", "blk"]).agg(pl.len().alias("k"), pl.col("fc_mm").n_unique().alias("nu"), pl.col("fc_mm").max().alias("mx"))
        blk = blk.filter((pl.col("k") == 3) & (pl.col("mx") > 0))
        r["share_3h_block_constant(pos)"] = float((blk["nu"] == 1).mean()) if blk.height else None
        rows.append(r)
    return pl.DataFrame(rows).sort(["model", "lead_days"])


def threshold_table(d: pl.DataFrame, keys) -> pl.DataFrame:
    rows = []
    for key, g in d.group_by(keys, maintain_order=True):
        y = g["obs_wet"].to_list()
        n_obs = sum(y)
        for th in CANDIDATES:
            pred = (g["fc_mm"] >= th).to_list()
            p, pod, pofd, hits, misses, fa, cn = skill.pss(pred, y)
            n_pred = hits + fa
            rows.append({**dict(zip(keys, key)), "threshold_mm": th, "n": len(y), "obs_wet_rate": n_obs / len(y),
                         "n_fc_wet": n_pred, "bias": (n_pred / n_obs) if n_obs else None, "pod": pod,
                         "far": (fa / n_pred) if n_pred else None, "pofd": pofd, "pss": p})
    return pl.DataFrame(rows).sort(keys + ["threshold_mm"])


def block3h_table(d: pl.DataFrame, model: str = "ecmwf_ifs025", leads=(1, 3, 7)) -> pl.DataFrame:
    """ECMWF をネイティブの 3h ブロックで比較: fc3h = 毎時値 × 3 (= 3h 積算)、obs3h = ブロック内の実況合計。
    閾値は 3h 積算 mm (0.3 = 本体の毎時 0.1 相当)。timing 誤差の影響を 3h に均した見方。"""
    g = d.filter((pl.col("model") == model) & pl.col("lead_days").is_in(list(leads)))
    g = g.with_columns(block_key())
    b = g.group_by(["pair", "lead_days", "blk"]).agg(pl.len().alias("k"), pl.col("fc_mm").max().alias("fc_h"),
                                                            pl.col("precipitation1h").sum().alias("obs3h"))
    b = b.filter(pl.col("k") == 3).with_columns((pl.col("fc_h") * 3).round(1).alias("fc3h"))
    rows = []
    for (lead,), gg in b.group_by(["lead_days"], maintain_order=True):
        y = (gg["obs3h"] >= OBS_WET_MM).to_list()
        for th in [0.3, 0.5, 0.6, 0.9, 1.0, 1.5, 2.0, 3.0]:
            pred = (gg["fc3h"] >= th - 1e-6).to_list()
            p_, pod, pofd, hits, misses, fa, cn = skill.pss(pred, y)
            n_pred = hits + fa
            rows.append({"lead_days": lead, "fc3h_threshold_mm": th, "≈hourly": round(th / 3, 2), "n_blocks": len(y), "obs_wet_rate": sum(y) / len(y),
                         "n_fc_wet": n_pred, "bias": n_pred / sum(y) if sum(y) else None, "pod": pod, "far": fa / n_pred if n_pred else None, "pss": p_})
    return pl.DataFrame(rows).sort(["lead_days", "fc3h_threshold_mm"])


def tolerance_far(d: pl.DataFrame, keys) -> pl.DataFrame:
    """本体基準 (≥0.1) の FAR を、実況を T±1h に広げた場合と比べる (timing 誤差の寄与)。"""
    parts = []
    for (pair, model, lead), g in d.group_by(["pair", "model", "lead_days"], maintain_order=True):
        g = g.sort("valid_time")
        o = g.select("valid_time", "obs_wet")
        prev = o.with_columns((pl.col("valid_time") + pl.duration(hours=1)).alias("valid_time"), pl.col("obs_wet").alias("w_prev")).drop("obs_wet")
        nxt = o.with_columns((pl.col("valid_time") - pl.duration(hours=1)).alias("valid_time"), pl.col("obs_wet").alias("w_next")).drop("obs_wet")
        g = g.join(prev, on="valid_time", how="left").join(nxt, on="valid_time", how="left")
        g = g.with_columns((pl.col("obs_wet") | pl.col("w_prev").fill_null(False) | pl.col("w_next").fill_null(False)).alias("obs_wet_pm1"))
        parts.append(g)
    d2 = pl.concat(parts)
    w = d2.filter(pl.col("fc_mm") >= BODY_THRESHOLD)
    return (w.group_by(keys, maintain_order=True)
             .agg(pl.len().alias("n_fc_wet"), (1 - pl.col("obs_wet").mean()).alias("far_exact_hour"), (1 - pl.col("obs_wet_pm1").mean()).alias("far_pm1h"))
             .sort(keys))


def report(fc_path: Path = FORECAST_LONG, obs_path: Path = OBS_LONG) -> str:
    fc = pl.read_parquet(fc_path)
    obs = pl.read_parquet(obs_path)
    d = pairs(fc, obs)
    span = d.group_by("pair").agg(pl.col("valid_time").min().alias("first"), pl.col("valid_time").max().alias("last"), pl.len().alias("n")).sort("pair")
    pl.Config.set_tbl_width_chars(300); pl.Config.set_tbl_cols(40)
    parts = [
        "## P-0 標本 (pair × 期間; 全時間帯, obs = アメダス 1 時間降水量, wet = ≥0.5mm)\n" + fmt(span),
        "## P-1 本体基準 (予報 ≥0.1mm/h) で降水ありとした時間のうち実況 0.0mm (<0.5mm) だった割合 — モデル × lead (5 地点プール)\n"
        + fmt(false_alarm_at_body_threshold(d, ["model", "lead_days"]), 2),
        "## P-2 同 地点別 (lead 1)\n" + fmt(false_alarm_at_body_threshold(d.filter(pl.col("lead_days") == 1), ["pair", "model"]), 2),
        "## P-3 予報値の分布 (全時間) と ECMWF の 3h ブロック内一致率\n" + fmt(value_distribution(d), 3),
        "## P-4 閾値候補ごとの分割表 (5 地点プール): bias = 予報降水時間数/実況降水時間数, POD, FAR = 予報降水のうち実況 0.0, PSS\n"
        + fmt(threshold_table(d, ["model", "lead_days"]), 3),
        "## P-5 同 地点別 (lead 1, ECMWF と MSM)\n" + fmt(threshold_table(d.filter(pl.col("lead_days") == 1), ["pair", "model"]), 3),
        "## P-6 ECMWF を 3h ブロックで比較 (fc3h = 毎時値×3 = ネイティブ 3h 積算, obs3h = 実況 3h 合計 ≥0.5mm)\n" + fmt(block3h_table(d), 3),
        "## P-7 本体基準 (≥0.1) の FAR: 実況を T±1h に広げた場合 (timing 誤差の寄与)\n" + fmt(tolerance_far(d, ["model", "lead_days"]), 3),
    ]
    return "\n\n".join(parts)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=OUT_PATH)
    args = ap.parse_args(argv)
    text = report()
    args.out.write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
