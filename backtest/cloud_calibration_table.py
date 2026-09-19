"""本体に載せる静的テーブル (R1): 山頂雲量の生値ビン → P(晴れ)。麓 4 地点プール (LOO ではなく全データ)、暖候期標本。

- ビン: 0% (塊) / (0,10] / (10,20] / (20,30] / (30,40] / (40,50] / (50,70] / (70,100]。上位ビンは標本が薄い
  (lead 別だと 70% 超は 1〜55 件) ので (50,70] と (70,100] に束ね、さらに lead も束ねる:
  ecmwf d1-2 / d3-5 / d6-10、jma_msm d1-2 / d3-4。ビンごとの n を出力に残す。
- 値は quantile_map / trackb_eval と同じ value_h1 (T-1h, T の平均)。本体は毎時値に適用する (差は小さいが記録)。
- 出力: docs/cloud-calibration-table.json (本体へコピーする元) と docs/cloud-calibration-table.txt (n 入り)。

    python -m backtest.cloud_calibration_table
"""
import argparse
import json
from datetime import date
from pathlib import Path

import polars as pl

from . import config
from .quantile_map import pav_decreasing
from .trackb_eval import OBS_PATH, SR_PATH, fmt, lead_day_pairs

BIN_EDGES = [0, 10, 20, 30, 40, 50, 70, 100]      # 0 は塊として別扱い、以降は (lo, hi]
BIN_LABELS = ["0"] + [f"({BIN_EDGES[i]},{BIN_EDGES[i + 1]}]" for i in range(len(BIN_EDGES) - 1)]
LEAD_GROUPS = {"ecmwf_ifs025": [(1, 2), (3, 5), (6, 10)], "jma_msm": [(1, 2), (3, 4)]}
MIN_N = 30
OUT_JSON = config.DATA_DIR.parent / "docs" / "cloud-calibration-table.json"
OUT_TXT = config.DATA_DIR.parent / "docs" / "cloud-calibration-table.txt"


def bin_of(v: float) -> int:
    if v <= 0:
        return 0
    for i, hi in enumerate(BIN_EDGES[1:], start=1):
        if v <= hi:
            return i
    return len(BIN_EDGES) - 1


def build(sr: pl.DataFrame, obs: pl.DataFrame) -> tuple[dict, pl.DataFrame]:
    d = lead_day_pairs(sr, obs, "cloud_cover_at_summit").filter(~pl.col("pair").str.starts_with("fuji"))
    d = d.with_columns(pl.col("value_h1").map_elements(bin_of, return_dtype=pl.Int64).alias("bin"))
    table, rows = {}, []
    for model, groups in LEAD_GROUPS.items():
        for lo, hi in groups:
            g = d.filter((pl.col("model") == model) & (pl.col("lead_day") >= lo) & (pl.col("lead_day") <= hi))
            p_all = float(g["obs_sunny"].mean())
            entry = {"lead_days": [lo, hi], "n": g.height, "p_sunny_all": round(p_all, 3), "bins": []}
            for b, label in enumerate(BIN_LABELS):
                gb = g.filter(pl.col("bin") == b)
                n = gb.height
                p = float(gb["obs_sunny"].mean()) if n else None
                thin = n < MIN_N
                entry["bins"].append({"bin": label, "n": n, "p_sunny": (round(p, 3) if p is not None else None), "thin": thin})
                rows.append({"model": model, "lead": f"d{lo}-{hi}", "bin": label, "n": n, "p_sunny": p, "thin": thin,
                             "share_of_hours": n / g.height if g.height else None})
            # 使う値: n 重み付き PAV で雲量について単調非増加に均す (薄いビンや標本ノイズの逆転を隣と併合)。
            used = pav_decreasing([e["p_sunny"] if e["p_sunny"] is not None else 0.0 for e in entry["bins"]],
                                  [max(e["n"], 1) for e in entry["bins"]])
            for e, u in zip(entry["bins"], used):
                e["p_sunny_used"] = round(u, 3)
            table.setdefault(model, []).append(entry)
    return table, pl.DataFrame(rows)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-json", type=Path, default=OUT_JSON)
    ap.add_argument("--out-txt", type=Path, default=OUT_TXT)
    args = ap.parse_args(argv)
    sr = pl.read_parquet(SR_PATH)
    obs = pl.read_parquet(OBS_PATH)
    table, rows = build(sr, obs)
    meta = {
        "built": date.today().isoformat(),
        "source": "yamabiyori-backtest: Single Runs 00Z 2026-06-12..09-17 (warm season only), cloud_cover_at_summit (T-1h/T mean), "
                  "foot AMeDAS sunshine (sun1h>=0.4h) at 白馬/野辺山/奥日光/鷲倉, daytime 07-17 JST; validated summits 1700-2899m",
        "bin_edges": BIN_EDGES, "bin_labels": BIN_LABELS, "min_n": MIN_N,
        "note": "p_sunny_used = p_sunny after n-weighted PAV (monotone non-increasing in cloud). Apply per hour, before any window averaging.",
        "table": table,
    }
    args.out_json.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    pl.Config.set_tbl_rows(200); pl.Config.set_tbl_width_chars(200)
    txt = ("## 生値ビン → P(晴れ) テーブル (麓 4 地点プール, 暖候期 2026-06-12〜09-17, 日中, 山頂 1700〜2899m; thin = n<30; 使う値は n 重み付き PAV で単調化, JSON の p_sunny_used)\n"
           + fmt(rows, 3))
    args.out_txt.write_text(txt, encoding="utf-8")
    print(txt)


if __name__ == "__main__":
    main()
