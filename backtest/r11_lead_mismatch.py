"""R11: 本体 v1.5.0 の lead の数え方 (取得日を 1 とする暦日差) と、R1 表の学習時の数え方 (ラン初期時刻からの経過時間 // 24 + 1)
の違いで、本体が学習時と違う行 (model × lead 群) を引くケースを数える。

本体は Forecast API から「どのランの値か」を受け取らない (api-findings §10.1: レスポンスは複数ランの継ぎ足し)。
そこで、学習に使った Single Runs の (ラン, valid_time) の組をそのまま使い、
「本体がそのランの値を、ある日 (today) に取得した」と仮定したときに本体が引く行を計算して、学習時の行と比べる。
ラン時刻は学習と同じ 00Z に加え、00Z の値を時刻だけずらした 06/12/18Z の仮想ラン (Forecast API が実際に返しうるラン) も数える。
値は使わない (時刻の算術だけ)。today は「ランの初期時刻の JST 日付」(+0) と「その翌日」(+1)。
+0 と +1 のどちらが起こるかは取得時刻とランの公開遅れで決まる (00Z の ecmwf は 07:44Z 公開の実測 1 件のみ、§10.1) → 未確認なので両方出す。

    python -m backtest.r11_lead_mismatch [--out docs/r11-lead-mismatch.txt]
"""
import argparse
from datetime import timedelta
from pathlib import Path

import polars as pl

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
    args.out.write_text(txt, encoding="utf-8")
    print(txt)


if __name__ == "__main__":
    main()
