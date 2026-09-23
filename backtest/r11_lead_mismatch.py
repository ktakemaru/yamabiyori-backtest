"""R11: 本体 v1.5.0 の lead の数え方 (取得日を 1 とする暦日差) と、R1 表の学習時の数え方 (ラン初期時刻からの経過時間 // 24 + 1)
の違いで、本体が学習時と違う行 (model × lead 群) を引くケースを数える。

本体は Forecast API から「どのランの値か」を受け取らない (api-findings §10.1: レスポンスは複数ランの継ぎ足し)。
そこで、学習に使った Single Runs の (ラン, valid_time) の組をそのまま使い、
「本体がそのランの値を、ある日 (today) に取得した」と仮定したときに本体が引く行を計算して、学習時の行と比べる。
ラン時刻は学習と同じ 00Z に加え、00Z の値を時刻だけずらした 06/12/18Z の仮想ラン (Forecast API が実際に返しうるラン) も数える。
行の数え上げは時刻の算術だけ。today は「ランの初期時刻の JST 日付」(+0) と「その翌日」(+1)。
+0 と +1 のどちらが起こるかは取得時刻とランの公開遅れで決まる (00Z の ecmwf は 07:44Z 公開の実測 1 件のみ、§10.1) → 未確認なので両方出す。

影響の大きさ (impact): 同じ組の山頂雲量の値 (00Z ラン、毎時の瞬時値、5 座) に、学習時の行と本体の行の両方で R1 表を当てて
P(晴れ) と実効雲量 (案C) の差を出し、さらに本体 v1.5.0 の窓 (稜線帯 = 日の出+1h〜12 時の平均、PM = 12 時〜日没+30 分の 2 時間持続ピーク、
その大きい方) でスコアの雲量項 100·(1 − c/100)^0.25 の差を出す (mountain_climb_score の雲量の因子。視程・降水の因子は 1 以下なので、
実際のスコア差はこの値以下、比は視程・降水によらず同じ)。仮想ラン (06/12/18Z) の値は 00Z の値の流用 (値の分布の代用)。

    python -m backtest.r11_lead_mismatch [--out docs/r11-lead-mismatch.txt]
"""
import argparse
import json
import math
from datetime import date, datetime, timedelta
from pathlib import Path

import polars as pl

import mos

from . import config
from .cloud_calibration_table import LEAD_GROUPS
from .parse_single_runs import PARQUET_PATH as SR_PATH
from .trackb_eval import DAY_HOURS_JST, fmt

OUT = config.DATA_DIR.parent / "docs" / "r11-lead-mismatch.txt"
RUN_SHIFTS_H = [0, 6, 12, 18]


def group_of(model: str, lead_day: int) -> str:
    """本体 cloud_calibration_row と同じ選び方 (範囲内 → その群、範囲外 → 最も近い群、同距離は先の群)。"""
    groups = LEAD_GROUPS[model]
    for lo, hi in groups:
        if lo <= lead_day <= hi:
            return f"d{lo}-{hi}"
    lo, hi = min(groups, key=lambda g: min(abs(lead_day - g[0]), abs(lead_day - g[1])))
    return f"d{lo}-{hi}"


def pairs(sr: pl.DataFrame) -> pl.DataFrame:
    """学習に使った (model, run_utc, valid_time) の組 (地点は時刻の算術に関係しないので重複を除く)。"""
    return (sr.filter((pl.col("variable") == "cloud_cover_at_summit") & pl.col("level_hpa").is_null() & pl.col("value").is_not_null())
            .select("model", "run_utc", "valid_time").unique())


def count(p: pl.DataFrame) -> pl.DataFrame:
    rows = []
    for model, run, vt in p.iter_rows():
        for shift in RUN_SHIFTS_H:
            r = run + timedelta(hours=shift)
            if vt < r:
                continue
            lead_h = int((vt - r).total_seconds() // 3600)
            train_ld = lead_h // 24 + 1
            v_date = (vt + timedelta(hours=9)).date()
            hour_jst = (vt + timedelta(hours=9)).hour
            run_date = (r + timedelta(hours=9)).date()
            for off in (0, 1):
                today = run_date + timedelta(days=off)
                if v_date < today:          # 本体の系列は today 00:00 JST から始まる
                    continue
                body_ld = max(1, (v_date - today).days + 1)
                rows.append((model, f"{shift:02d}Z", f"+{off}", hour_jst in DAY_HOURS_JST,
                             train_ld != body_ld, group_of(model, train_ld) != group_of(model, body_ld)))
    d = pl.DataFrame(rows, schema=["model", "run", "today", "daytime", "lead_day_differs", "row_differs"], orient="row")
    return d


def summarize(d: pl.DataFrame, keys: list[str]) -> pl.DataFrame:
    return (d.group_by(keys).agg(pl.len().alias("n_hours"),
                                 pl.col("lead_day_differs").mean().alias("lead_day_differs"),
                                 pl.col("row_differs").sum().alias("n_row_differs"),
                                 pl.col("row_differs").mean().alias("row_differs"))
            .sort(keys))


ECMWF_LATENCY_H = 7 + 44 / 60      # api-findings §10.1: 00Z ラン 07:43:53Z 公開 (2026-09-18 の実測 1 件)
ECMWF_CYCLE_H = 6                  # 同: update_interval 21600s


def ecmwf_weights(latency_h: float = ECMWF_LATENCY_H, cycle_h: int = ECMWF_CYCLE_H) -> dict:
    """取得時刻が一様で、どのランも公開遅れ latency_h で出て、次のランが出るまで系列全体がそのランの値だと仮定したときの
    (run, today) の時間比。1 分刻みで数える。継ぎ足し (06/18Z の短いホライズンの先は古いランの値) は無視している。"""
    w = {}
    for shift in RUN_SHIFTS_H:
        for m in range(cycle_h * 60):
            f_utc_h = shift + latency_h + m / 60          # ラン初期時刻の日の 00Z からの時間
            off = int((f_utc_h + 9) // 24) - int((shift + 9) // 24)
            key = (f"{shift:02d}Z", f"+{off}")
            w[key] = w.get(key, 0) + 1 / (cycle_h * 60 * len(RUN_SHIFTS_H))
    return w


def weighted_estimate(day: pl.DataFrame, model: str, weights: dict) -> float:
    s = summarize(day.filter(pl.col("model") == model), ["run", "today"])
    by = {(r, t): v for r, t, v in s.select("run", "today", "row_differs").iter_rows()}
    return sum(wt * by[k] for k, wt in weights.items() if k in by) / sum(wt for k, wt in weights.items() if k in by)


# ---------------------------------------------------------------- 影響の大きさ
TABLE_PATH = config.DATA_DIR.parent / "mos_tables" / "r1-summit-cloud-sunny.json"
JST = timedelta(hours=9)
RIDGE_START_AFTER_SUNRISE_H = 1          # 本体 mvp: main_start = sunrise + TRIP_START_OFFSET_HOURS(-1)、ridge_start = main_start + RIDGE_DWELL_TRIM_HOURS(2)
RIDGE_END_HOUR = 12                      # 本体 mvp: MAIN_TIME_END_HOUR(14) − RIDGE_DWELL_TRIM_HOURS(2)
PM_START_HOUR = 12                       # 本体 mvp: PM_START_HOUR
ACTIVITY_END_GRACE_MIN = 30              # 本体: ACTIVITY_END_GRACE_MINUTES
PM_PERSIST_H = 2                         # 本体 core: PM_CLOUD_PERSIST_HOURS
CLOUD_WEIGHT = 0.25                      # 本体 core: SCORE_WEIGHTS["cloud"]


def sun_times_jst(d: date, lat: float, lon: float) -> tuple[datetime, datetime]:
    """日の出・日の入り (JST, naive)。NOAA の近似式 (天頂角 90.833°)。誤差は数分程度。"""
    g = 2 * math.pi / 365 * (d.timetuple().tm_yday - 1)
    eqt = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g) - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g) - 0.006758 * math.cos(2 * g) + 0.000907 * math.sin(2 * g)
            - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    la = math.radians(lat)
    ha = math.degrees(math.acos(math.cos(math.radians(90.833)) / (math.cos(la) * math.cos(decl)) - math.tan(la) * math.tan(decl)))
    base = datetime(d.year, d.month, d.day)
    rise = base + timedelta(minutes=720 - 4 * (lon + ha) - eqt + 540)
    sett = base + timedelta(minutes=720 - 4 * (lon - ha) - eqt + 540)
    return rise, sett


def sustained_peak(vals: list, hours: int = PM_PERSIST_H):
    """本体 core.sustained_peak と同じ。"""
    clean = [v for v in vals if v is not None]
    if not clean:
        return None
    if len(clean) <= hours:
        return sum(clean) / len(clean)
    return max(sum(clean[i:i + hours]) / hours for i in range(len(clean) - hours + 1))


def cloud_term(c: float) -> float:
    return 100.0 * max(0.0, 1 - c / 100.0) ** CLOUD_WEIGHT


class Lookup:
    """(model, lead_hours, value) → (p, 実効雲量, table_id)。mos の表をそのまま使い、同じ引数はキャッシュ。"""
    def __init__(self, ts):
        self.ts, self.tables = ts, {}

    def table(self, model: str, lead_hours: int):
        key = (model, lead_hours)
        if key not in self.tables:
            self.tables[key] = mos.select_table(self.ts, predictor="cloud_cover_at_summit", target="p_sunny", model=model,
                                                lead_hours=lead_hours, hour_jst=12, month=7).table
        return self.tables[key]

    def __call__(self, model: str, lead_hours: int, v: float):
        t = self.table(model, lead_hours)
        return mos.probability(self.ts, t, v), mos.effective_value(self.ts, t, v), t.table_id


def impact(sr: pl.DataFrame, ts) -> tuple[pl.DataFrame, pl.DataFrame]:
    f = (sr.filter((pl.col("variable") == "cloud_cover_at_summit") & pl.col("level_hpa").is_null() & pl.col("value").is_not_null())
         .select("site_id", "model", "run_utc", "valid_time", "value"))
    series = {}
    for site, model, run, vt, v in f.iter_rows():
        series.setdefault((site, run), {}).setdefault(vt, {})[model] = float(v)
    look = Lookup(ts)
    sites = {s["site_id"]: s for s in config.SITES}
    sun_cache = {}
    hours, days = [], []
    for (site, run), by_vt in series.items():
        s = sites[site]
        for shift in RUN_SHIFTS_H:
            r = run + timedelta(hours=shift)
            run_date = (r + JST).date()
            for off in (0, 1):
                today = run_date + timedelta(days=off)
                per_day = {}
                for vt in sorted(by_vt):
                    if vt < r:
                        continue
                    local = (vt + JST).replace(tzinfo=None)
                    if local.date() < today:
                        continue
                    vals = by_vt[vt]
                    model = "jma_msm" if "jma_msm" in vals else "ecmwf_ifs025"       # 本体: MSM の値がある時間は MSM
                    v = vals[model]
                    train_lh = int((vt - r).total_seconds() // 3600)
                    body_lh = 24 * max(0, (local.date() - today).days)
                    p_t, e_t, id_t = look(model, train_lh, v)
                    p_b, e_b, id_b = look(model, body_lh, v)
                    differs = id_t != id_b
                    if differs:
                        hours.append((model, f"{shift:02d}Z", f"+{off}", local.hour in DAY_HOURS_JST, v, p_b - p_t, e_b - e_t))
                    per_day.setdefault(local.date(), []).append((local, e_t, e_b, differs))
                for d, rows in per_day.items():
                    if (d, site) not in sun_cache:
                        sun_cache[(d, site)] = sun_times_jst(d, s["lat"], s["lon"])
                    rise, sett = sun_cache[(d, site)]
                    ridge_start = rise + timedelta(hours=RIDGE_START_AFTER_SUNRISE_H)
                    ridge_end = datetime(d.year, d.month, d.day, RIDGE_END_HOUR)
                    pm_end = sett + timedelta(minutes=ACTIVITY_END_GRACE_MIN)
                    ridge = [x for x in rows if ridge_start <= x[0] < ridge_end]
                    pm = [x for x in rows if x[0].hour >= PM_START_HOUR and x[0] < pm_end]
                    # 窓の時間がこのランで全部そろう日だけ (ラン初期時刻より前・ホライズンより先で欠ける日は除く)
                    need_ridge = sum(1 for h in range(24) if ridge_start <= datetime(d.year, d.month, d.day, h) < ridge_end)
                    need_pm = sum(1 for h in range(24) if h >= PM_START_HOUR and datetime(d.year, d.month, d.day, h) < pm_end)
                    if len(ridge) != need_ridge or len(pm) != need_pm or not ridge:
                        continue
                    c = []
                    for k in (1, 2):
                        rv = sum(x[k] for x in ridge) / len(ridge)
                        pv = sustained_peak([x[k] for x in pm])
                        c.append(max(rv, pv if pv is not None else 0.0))
                    any_diff = any(x[3] for x in ridge + pm)
                    days.append((site, f"{shift:02d}Z", f"+{off}", any_diff, c[0], c[1], cloud_term(c[1]) - cloud_term(c[0]),
                                 (cloud_term(c[1]) / cloud_term(c[0])) if cloud_term(c[0]) > 0 else None))
    h = pl.DataFrame(hours, schema=["model", "run", "today", "daytime", "value", "dp", "d_eff"], orient="row")
    dd = pl.DataFrame(days, schema=["site_id", "run", "today", "any_row_differs", "cloud_train", "cloud_body", "d_cloud_term", "ratio"],
                      orient="row")
    return h, dd


def impact_hours_summary(h: pl.DataFrame, keys: list[str]) -> pl.DataFrame:
    a = pl.col("dp").abs()
    e = pl.col("d_eff").abs()
    return (h.group_by(keys).agg(pl.len().alias("n_hours_row_differs"), a.mean().alias("mean_abs_dp"), a.max().alias("max_abs_dp"),
                                 e.mean().alias("mean_abs_d_eff"), e.quantile(0.9).alias("p90_abs_d_eff"), e.max().alias("max_abs_d_eff"),
                                 (e >= 10).mean().alias("share_d_eff_ge10"), pl.col("d_eff").mean().alias("mean_d_eff"))
            .sort(keys))


def impact_days_summary(dd: pl.DataFrame, keys: list[str]) -> pl.DataFrame:
    a = pl.col("d_cloud_term").abs()
    return (dd.group_by(keys).agg(pl.len().alias("n_site_days"), pl.col("any_row_differs").mean().alias("share_days_affected"),
                                  a.mean().alias("mean_abs_d_score"), a.quantile(0.9).alias("p90_abs_d_score"), a.max().alias("max_abs_d_score"),
                                  (a >= 3).mean().alias("share_ge3pt"), (a >= 5).mean().alias("share_ge5pt"),
                                  pl.col("d_cloud_term").mean().alias("mean_d_score"), pl.col("ratio").min().alias("min_ratio"))
            .sort(keys))


def weighted_days_line(dd: pl.DataFrame, weights: dict) -> str:
    s = impact_days_summary(dd, ["run", "today"])
    by = {(r, t): row for r, t, row in zip(s["run"], s["today"], s.iter_rows(named=True))}
    tot = sum(w for k, w in weights.items() if k in by)
    out = []
    for col in ["share_days_affected", "mean_abs_d_score", "share_ge3pt", "share_ge5pt", "mean_d_score"]:
        out.append(f"{col} ≈ {sum(w * by[k][col] for k, w in weights.items() if k in by) / tot:.3f}")
    return ", ".join(out) + " (MSM の時間も同じ重みで平均している点は近似)"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--sr", type=Path, default=SR_PATH)
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args(argv)
    d = count(pairs(pl.read_parquet(args.sr)))
    day = d.filter(pl.col("daytime"))
    txt = ("## R11 lead の数え方の違いで本体が学習時と違う行を引く割合\n"
           "入力: data/parquet/single_runs_long.parquet の cloud_cover_at_summit (00Z ラン 2026-06-12〜09-17) の (model, run, valid_time)。\n"
           "run = 本体が使ったと仮定するランの初期時刻 (00Z 以外は 00Z の valid_time の組を流用した仮想ラン)。\n"
           "today = 本体の取得日 (JST) がランの初期時刻の JST 日付と同じ (+0) / 翌日 (+1)。本体の系列は today 00 時 JST から。\n"
           "lead_day_differs = 本体 lead_day ≠ 学習 lead_day の割合、row_differs = 引く行 (lead 群) が違う割合。\n\n"
           "### 日中 (JST 07〜17 時に終わる 1 時間 = 表の学習範囲)\n" + fmt(summarize(day, ["model", "run", "today"]), 3)
           + "\n\n### 全時刻 (本体は夜間も適用)\n" + fmt(summarize(d, ["model", "run", "today"]), 3)
           + "\n\n### 参考: ecmwf_ifs025 の取得時刻で重み付けした日中の row_differs (仮定つき)\n"
           + f"仮定: 全ランの公開遅れ {ECMWF_LATENCY_H:.2f}h (実測は 00Z の 1 件のみ)、{ECMWF_CYCLE_H}h ごとにラン、取得時刻は一様、"
             "系列全体が最新ランの値 (継ぎ足しを無視)。\n"
           + "重み (run, today): " + ", ".join(f"{r}{t}={v:.3f}" for (r, t), v in sorted(ecmwf_weights().items())) + "\n"
           + f"row_differs (日中) ≈ {weighted_estimate(day, 'ecmwf_ifs025', ecmwf_weights()):.3f}\n"
           + "jma_msm は 3h ごとのランでホライズンが 39h / 78h と交互 (§10.1) のため、この重み付けは行っていない (未計算)。\n")
    sr = pl.read_parquet(args.sr)
    ts = mos.parse_table_set(json.loads(TABLE_PATH.read_text(encoding="utf-8")))
    h, dd = impact(sr, ts)
    hd = h.filter(pl.col("daytime"))
    txt += ("\n\n## 影響の大きさ (2026-09-23 追記)\n"
            "値: single_runs_long の cloud_cover_at_summit (00Z ラン、毎時の瞬時値、5 座)。本体と同じく MSM の値がある時間は jma_msm、それ以外は ecmwf_ifs025。\n"
            "06/12/18Z の仮想ランは 00Z の値を流用 (値の分布の代用、実際のランの値ではない)。表は mos_tables/r1-summit-cloud-sunny.json (= 本体 v1.5.0 の表)。\n"
            "dp = P(晴れ) の差 (本体の行 − 学習の行)、d_eff = 実効雲量 (案C, %) の差。正 = 本体の方が曇り寄り (辛い)。\n\n"
            "### 行が違う時間の値の差 (日中)\n" + fmt(impact_hours_summary(hd, ["model", "run", "today"]), 3)
            + "\n\n### 同 (日中、ラン・取得日をまとめて)\n" + fmt(impact_hours_summary(hd, ["model"]), 3)
            + "\n\n### スコアへの影響 (山日単位)\n"
            "本体 v1.5.0 の窓 (稜線帯 = 日の出+1h〜12 時の平均、PM = 12 時〜日没+30 分の 2 時間持続ピーク、大きい方) で雲量入力 c を作り、\n"
            "スコアの雲量項 100·(1 − c/100)^0.25 の差 (本体の行 − 学習の行) を出した。実際のスコア = 雲量項 × 視程・降水の因子 (各 ≤ 1) なので、\n"
            "実際のスコア差の絶対値はこの値以下、比 (min_ratio = 本体/学習 の最小) は視程・降水によらない。日の出・日の入りは NOAA 近似 (誤差数分)。\n"
            "窓の時間がそのランで全部そろう山日だけ (n_site_days)。share_days_affected = 窓の中に行が違う時間を含む山日の割合。\n"
            + fmt(impact_days_summary(dd, ["run", "today"]), 3)
            + "\n\n### 同 (行が違う時間を含む山日だけ)\n" + fmt(impact_days_summary(dd.filter(pl.col("any_row_differs")), ["run", "today"]), 3)
            + "\n\n### 参考: 上の ecmwf の取得時刻の重み (公開遅れ 7.73h 仮定) で山日単位の影響を平均\n"
            + weighted_days_line(dd, ecmwf_weights()) + "\n")
    args.out.write_text(txt, encoding="utf-8")
    print(txt)


if __name__ == "__main__":
    main()
