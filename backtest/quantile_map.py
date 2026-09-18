"""分位点マッピング (山頂雲量 → 「晴れる確率」) の実装と leave-one-site-out 評価。**本体には入れない**。暖候期 (2026-06〜09) のみ。

設計 (track-b-findings §7.1〜7.4 の判断をそのまま実装):
  - 入力 x = 診断山頂雲量 (T-1h, T の平均, 0〜100)。0% の塊は 1 つの値なので、
      x == 0        → 経験確率 1 点  P(晴れ | x=0)  (訓練地点プール)
      x  > 0        → 地点ごとの正値の経験 CDF で分位 q = F_site(x) に直し、q の 10 分位ビンごとの晴れ率 (訓練地点プール) に写像。
                      ビン平均は PAV (pool-adjacent-violators) で q について単調非増加に均す。
    地点側の CDF は**予報値だけ**から作る (実況不要) ので、実況の無い山にも適用できる = 本体 76 座に載せられる形。
  - 層別: model × lead_day (1, 2, 3, 5, 7, 10)。季節は層別しない (暖候期のデータしか無い)。
  - 分割: leave-one-site-out (5 地点)。富士山を検証側にした回は「山頂実況」なので別枠。
  - 比較対象: (a) raw = 1 − x/100 をそのまま確率とみなす、(b) clim = 訓練地点プールの晴れ率 (定数)、
    (c) rawbin = 分位変換せず生値のビン (0 / (0,10] / (10,20] / … / (90,100]) の晴れ率 (「分位変換が地点間転移に効くか」の対照)、
    (d) qmap_total = 同じ写像を地上全雲量 (cloud_cover) に適用。
  - 指標: Brier score (と reliability / resolution / uncertainty 分解, 10 ビン)、信頼度図 (ビンごとの予報確率平均・実況晴れ率・n)、
    補助として PSS@0.5。AUC は単調変換で不変なので出さない (rawbin / PAV は同点を作るのでわずかに変わるが無視)。

    python -m backtest.quantile_map            # docs/quantile-map-tables.txt を書く
"""
import argparse
import math
from pathlib import Path

import polars as pl
pl.Config.set_tbl_width_chars(260)
pl.Config.set_fmt_str_lengths(120)
pl.Config.set_fmt_table_cell_list_len(12)

from . import config
from .trackb_eval import OBS_PATH, SR_PATH, fmt, lead_day_pairs

LEAD_DAYS = [1, 2, 3, 5, 7, 10]
N_QBINS = 10
RELIAB_BINS = [i / 10 for i in range(11)]
MIN_TRAIN_ZERO = 20     # 0% 塊の経験確率に最低限必要な訓練 n (下回ればプール全体の晴れ率で代用)
OUT_PATH = config.DATA_DIR.parent / "docs" / "quantile-map-tables.txt"


# ---------------------------------------------------------------- 写像の学習と適用

def pav_decreasing(values: list[float], weights: list[float]) -> list[float]:
    """重み付き PAV: 入力順 (q の昇順) に対して単調非増加になるよう隣接ブロックを併合する。"""
    blocks = [[v, w, 1] for v, w in zip(values, weights)]   # [平均, 重み, 要素数]
    i = 0
    while i < len(blocks) - 1:
        if blocks[i][0] < blocks[i + 1][0]:     # 増加していたら併合
            v1, w1, n1 = blocks[i]; v2, w2, n2 = blocks[i + 1]
            w = w1 + w2
            blocks[i] = [(v1 * w1 + v2 * w2) / w if w else 0.0, w, n1 + n2]
            del blocks[i + 1]
            i = max(i - 1, 0)
        else:
            i += 1
    out = []
    for v, _, n in blocks:
        out.extend([v] * n)
    return out


def site_positive_cdf(x: pl.Series) -> list[float]:
    """地点の正値の経験 CDF を表すソート済み配列 (予報値のみ)。"""
    return x.filter(x > 0).sort().to_list()


def quantile_of(sorted_pos: list[float], v: float) -> float:
    """v (>0) の分位 (0..1)。同点は中央 (mid-rank)。"""
    import bisect
    if not sorted_pos:
        return 0.5
    lo = bisect.bisect_left(sorted_pos, v)
    hi = bisect.bisect_right(sorted_pos, v)
    return (lo + hi) / 2 / len(sorted_pos)


def qbin_of(q: float) -> int:
    return min(int(q * N_QBINS), N_QBINS - 1)


def fit_qmap(train: pl.DataFrame, cdfs: dict[str, list[float]]) -> dict:
    """train: (pair, value_h1, obs_sunny)。cdfs: pair → その地点の正値 CDF (予報のみ)。
    返り値 {"p_zero": float, "n_zero": int, "p_bin": [10 個], "n_bin": [10 個], "p_all": float}。"""
    p_all = train["obs_sunny"].mean()
    z = train.filter(pl.col("value_h1") == 0)
    n_zero = z.height
    p_zero = z["obs_sunny"].mean() if n_zero >= MIN_TRAIN_ZERO else p_all
    pos = train.filter(pl.col("value_h1") > 0)
    sums = [0.0] * N_QBINS
    cnts = [0] * N_QBINS
    for pair, v, y in zip(pos["pair"].to_list(), pos["value_h1"].to_list(), pos["obs_sunny"].to_list()):
        b = qbin_of(quantile_of(cdfs[pair], v))
        sums[b] += 1.0 if y else 0.0
        cnts[b] += 1
    raw = [(s / c if c else p_all) for s, c in zip(sums, cnts)]
    p_bin = pav_decreasing(raw, [max(c, 1) for c in cnts])
    return {"p_zero": p_zero, "n_zero": n_zero, "p_bin": p_bin, "p_bin_raw": raw, "n_bin": cnts, "p_all": p_all}


def apply_qmap(m: dict, cdf: list[float], v: float) -> float:
    if v == 0:
        return m["p_zero"]
    return m["p_bin"][qbin_of(quantile_of(cdf, v))]


RAWBIN_EDGES = [0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100]


def rawbin_of(v: float) -> int:
    if v == 0:
        return 0
    for i, hi in enumerate(RAWBIN_EDGES[1:], start=1):
        if v <= hi:
            return i
    return len(RAWBIN_EDGES) - 1


def fit_rawbin(train: pl.DataFrame) -> dict:
    p_all = train["obs_sunny"].mean()
    sums = [0.0] * len(RAWBIN_EDGES); cnts = [0] * len(RAWBIN_EDGES)
    for v, y in zip(train["value_h1"].to_list(), train["obs_sunny"].to_list()):
        b = rawbin_of(v); sums[b] += 1.0 if y else 0.0; cnts[b] += 1
    return {"p_bin": [(s / c if c else p_all) for s, c in zip(sums, cnts)], "n_bin": cnts, "p_all": p_all}


def apply_rawbin(m: dict, v: float) -> float:
    return m["p_bin"][rawbin_of(v)]


# ---------------------------------------------------------------- 指標

def brier(p: list[float], y: list[bool]) -> float:
    return sum((pi - (1.0 if yi else 0.0)) ** 2 for pi, yi in zip(p, y)) / len(p)


def brier_decomposition(p: list[float], y: list[bool], edges=RELIAB_BINS) -> dict:
    """Murphy 分解 (ビン = 予報確率の 10 等分)。BS = reliability − resolution + uncertainty (ビン内近似)。"""
    n = len(p)
    ybar = sum(1.0 for v in y if v) / n
    bins: dict[int, list[tuple[float, bool]]] = {}
    for pi, yi in zip(p, y):
        b = min(int(pi * (len(edges) - 1)), len(edges) - 2)
        bins.setdefault(b, []).append((pi, yi))
    rel = res = 0.0
    rows = []
    for b in sorted(bins):
        items = bins[b]; nk = len(items)
        pk = sum(t[0] for t in items) / nk
        ok = sum(1.0 for t in items if t[1]) / nk
        rel += nk * (pk - ok) ** 2
        res += nk * (ok - ybar) ** 2
        rows.append({"bin": f"[{edges[b]:.1f},{edges[b + 1]:.1f})", "n": nk, "forecast_p": pk, "obs_rate": ok})
    return {"brier": brier(p, y), "reliability": rel / n, "resolution": res / n, "uncertainty": ybar * (1 - ybar),
            "n": n, "obs_rate": ybar, "bins": rows}


def pss_at(p: list[float], y: list[bool], th: float = 0.5) -> float | None:
    from .skill import pss
    return pss([pi >= th for pi in p], y)[0]


# ---------------------------------------------------------------- leave-one-site-out

def build_pairs(sr: pl.DataFrame, obs: pl.DataFrame) -> dict[str, pl.DataFrame]:
    out = {}
    for pred in ["cloud_cover_at_summit", "cloud_cover"]:
        d = lead_day_pairs(sr, obs, pred)
        out[pred] = d.filter(pl.col("lead_day").is_in(LEAD_DAYS)) if not d.is_empty() else d
    return out


def site_cdfs(d: pl.DataFrame) -> dict[str, list[float]]:
    """地点 (pair) ごとの正値 CDF。model × lead_day の層ごとに呼ぶ (層内の予報値だけで作る)。"""
    return {pair: site_positive_cdf(g["value_h1"]) for (pair,), g in d.group_by(["pair"])}


def evaluate(pairs: dict[str, pl.DataFrame]) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    """返り値: (scores, reliability rows, fitted maps)。"""
    score_rows, rel_rows, map_rows = [], [], []
    d_summit, d_total = pairs["cloud_cover_at_summit"], pairs["cloud_cover"]
    for (model, lead), g in d_summit.group_by(["model", "lead_day"], maintain_order=True):
        gt = d_total.filter((pl.col("model") == model) & (pl.col("lead_day") == lead))
        cdfs = site_cdfs(g)
        cdfs_t = site_cdfs(gt) if not gt.is_empty() else {}
        for held in sorted(g["pair"].unique().to_list()):
            train = g.filter(pl.col("pair") != held)
            test = g.filter(pl.col("pair") == held)
            if test.height < 50 or train.height < 200:
                continue
            m = fit_qmap(train, cdfs)
            rb = fit_rawbin(train)
            y = test["obs_sunny"].to_list()
            xs = test["value_h1"].to_list()
            methods = {
                "qmap": [apply_qmap(m, cdfs[held], v) for v in xs],
                "rawbin": [apply_rawbin(rb, v) for v in xs],
                "raw": [1 - v / 100 for v in xs],
                "clim": [m["p_all"]] * len(xs),
            }
            # (d) 地上全雲量に同じ写像 (同じ検証事例に揃える)
            if not gt.is_empty() and held in cdfs_t:
                tt = gt.filter(pl.col("pair") == held).select("run_utc", "valid_time", pl.col("value_h1").alias("value_total"))
                tj = test.join(tt, on=["run_utc", "valid_time"], how="left")
                if tj["value_total"].null_count() == 0 and tj.height == test.height:
                    mt = fit_qmap(gt.filter(pl.col("pair") != held).rename({"value_h1": "value_h1"}), cdfs_t)
                    methods["qmap_total"] = [apply_qmap(mt, cdfs_t[held], v) for v in tj["value_total"].to_list()]
            for name, p in methods.items():
                dec = brier_decomposition(p, y)
                score_rows.append({"model": model, "lead_day": lead, "held_out": held, "method": name, "n": dec["n"],
                                   "obs_rate": dec["obs_rate"], "brier": dec["brier"], "reliability": dec["reliability"],
                                   "resolution": dec["resolution"], "uncertainty": dec["uncertainty"], "pss_0.5": pss_at(p, y)})
                if name in ("qmap", "raw", "rawbin"):
                    for r in dec["bins"]:
                        rel_rows.append({"model": model, "lead_day": lead, "held_out": held, "method": name, **r})
            map_rows.append({"model": model, "lead_day": lead, "held_out": held, "n_train": train.height,
                             "p_zero": m["p_zero"], "n_zero": m["n_zero"], "p_all": m["p_all"],
                             "p_bin": [round(v, 2) for v in m["p_bin"]], "n_bin": m["n_bin"],
                             "share_zero_heldout": float((test["value_h1"] == 0).mean())})
    return pl.DataFrame(score_rows), pl.DataFrame(rel_rows), pl.DataFrame(map_rows)


def summarize(scores: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame]:
    """麓 4 地点 (n 重み付き) と富士山頂を分けて、method × model × lead の Brier / PSS。"""
    def agg(df):
        return (df.group_by(["model", "lead_day", "method"], maintain_order=True)
                  .agg(pl.col("n").sum().alias("n"),
                       ((pl.col("brier") * pl.col("n")).sum() / pl.col("n").sum()).alias("brier"),
                       ((pl.col("reliability") * pl.col("n")).sum() / pl.col("n").sum()).alias("reliability"),
                       ((pl.col("resolution") * pl.col("n")).sum() / pl.col("n").sum()).alias("resolution"),
                       ((pl.col("uncertainty") * pl.col("n")).sum() / pl.col("n").sum()).alias("uncertainty"),
                       ((pl.col("pss_0.5") * pl.col("n")).sum() / pl.col("n").sum()).alias("pss_0.5"))
                  .sort(["model", "lead_day", "method"]))
    foot = scores.filter(~pl.col("held_out").str.starts_with("fuji"))
    fuji = scores.filter(pl.col("held_out").str.starts_with("fuji"))
    return agg(foot), (agg(fuji) if not fuji.is_empty() else pl.DataFrame())


def wide_brier(summary: pl.DataFrame) -> pl.DataFrame:
    if summary.is_empty():
        return summary
    return summary.pivot(on="method", index=["model", "lead_day"], values="brier").sort(["model", "lead_day"])


def report(sr_path: Path = SR_PATH, obs_path: Path = OBS_PATH) -> str:
    sr = pl.read_parquet(sr_path)
    obs = pl.read_parquet(obs_path)
    pairs = build_pairs(sr, obs)
    scores, rel, maps = evaluate(pairs)
    foot, fuji = summarize(scores)
    parts = [
        "## Q-0 標本: 暖候期 (2026-06-12〜09-17 の 00Z ラン)、日中 07〜17 JST、leave-one-site-out。本体には未反映。",
        "## Q-1 Brier score (小さいほど良い), 麓 4 地点を検証側にした回の n 重み付き平均。method: qmap=分位点写像 (0% 塊は経験確率 1 点), rawbin=生値ビン, raw=1−雲量/100, clim=訓練プール晴れ率, qmap_total=地上全雲量に同じ写像",
        fmt(wide_brier(foot)),
        "## Q-2 同 Brier 分解 (reliability 小さいほど良い / resolution 大きいほど良い) と PSS@0.5",
        fmt(foot.select(["model", "lead_day", "method", "n", "brier", "reliability", "resolution", "uncertainty", "pss_0.5"])),
        "## Q-3 富士山頂 (山頂実況, 7〜8 月) を検証側にした回",
        fmt(wide_brier(fuji)) if not fuji.is_empty() else "(no data)",
        fmt(fuji.select(["model", "lead_day", "method", "n", "brier", "reliability", "resolution", "pss_0.5"])) if not fuji.is_empty() else "",
        "## Q-4 地点別 (held_out) の Brier: qmap vs raw vs clim",
        fmt(scores.filter(pl.col("method").is_in(["qmap", "raw", "clim", "rawbin"]))
                  .pivot(on="method", index=["model", "lead_day", "held_out"], values="brier").sort(["model", "lead_day", "held_out"])),
        "## Q-5 学習された写像 (held_out ごと): p_zero = P(晴れ|0%) 訓練プール, p_bin = 正値の分位 10 ビン (q 昇順 = 雲量が増える向き) の晴れ率 (PAV 後)",
        fmt(maps.select(["model", "lead_day", "held_out", "n_train", "n_zero", "p_zero", "p_all", "share_zero_heldout", "p_bin", "n_bin"])),
        "## Q-6 信頼度図 (qmap / raw / rawbin; 予報確率 10 ビンごとの forecast_p 平均と obs_rate, n): 麓 4 地点プール, ecmwf d1 / d3 / d7",
    ]
    for lead in (1, 3, 7):
        r = rel.filter((pl.col("model") == "ecmwf_ifs025") & (pl.col("lead_day") == lead) & ~pl.col("held_out").str.starts_with("fuji"))
        if r.is_empty():
            continue
        pooled = (r.group_by(["method", "bin"], maintain_order=True)
                   .agg(pl.col("n").sum().alias("n"),
                        ((pl.col("forecast_p") * pl.col("n")).sum() / pl.col("n").sum()).alias("forecast_p"),
                        ((pl.col("obs_rate") * pl.col("n")).sum() / pl.col("n").sum()).alias("obs_rate"))
                   .sort(["method", "bin"]))
        parts.append(f"### ecmwf d{lead}\n" + fmt(pooled))
    return "\n\n".join(p for p in parts if p)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=OUT_PATH)
    args = ap.parse_args(argv)
    text = report()
    args.out.write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
