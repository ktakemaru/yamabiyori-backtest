"""閾値に依存しない識別性能: ROC AUC と Peirce skill score (Hanssen–Kuipers, PSS = POD − POFD)。
外部依存なし (polars + 純 Python)。

- auc(scores, labels): 「晴れ (label=True) のスコアが曇りのスコアより大きい確率」= Mann–Whitney U 統計量/(n1 n0)。
  同点は 0.5 でカウント。晴れを高スコアにしたいので、雲量を渡すときは符号を反転して渡す (−cloud)。
  DeLong 法の標準誤差も返す (Hanley & McNeil 1982 の近似ではなく DeLong の構造成分から)。
- pss(pred_pos, obs_pos): POD − POFD。常に同じ側を答える予報は 0、完全予報は 1。
- pss_max(scores, labels): 閾値を動かしたときの最大 PSS (= Youden's J) と、そのときの閾値。
  AUC が「順序」だけを見るのに対し、PSS_max は「最良の 1 本の閾値」での 2 値性能。
"""
import math


def auc(scores, labels):
    """scores: float list, labels: bool list (True = 陽性 = 晴れ)。返り値 (auc, se, n_pos, n_neg)。
    n_pos または n_neg が 0 なら (None, None, n_pos, n_neg)。"""
    pos = [s for s, l in zip(scores, labels) if l and s is not None]
    neg = [s for s, l in zip(scores, labels) if (not l) and s is not None]
    n1, n0 = len(pos), len(neg)
    if n1 == 0 or n0 == 0:
        return None, None, n1, n0
    neg_sorted = sorted(neg)
    # 各陽性について「それより小さい陰性の数 + 同点の半分」を二分探索で数える (O(n log n))
    import bisect
    v10 = []   # DeLong の構造成分 (陽性側)
    for s in pos:
        lo = bisect.bisect_left(neg_sorted, s)
        hi = bisect.bisect_right(neg_sorted, s)
        v10.append((lo + 0.5 * (hi - lo)) / n0)
    pos_sorted = sorted(pos)
    v01 = []   # 陰性側
    for s in neg:
        lo = bisect.bisect_left(pos_sorted, s)
        hi = bisect.bisect_right(pos_sorted, s)
        v01.append(((n1 - hi) + 0.5 * (hi - lo)) / n1)
    a = sum(v10) / n1
    s10 = sum((x - a) ** 2 for x in v10) / max(n1 - 1, 1)
    s01 = sum((x - a) ** 2 for x in v01) / max(n0 - 1, 1)
    se = math.sqrt(s10 / n1 + s01 / n0)
    return a, se, n1, n0


def pss(pred_pos, obs_pos):
    """Peirce skill score = POD − POFD。返り値 (pss, pod, pofd, hits, misses, false_alarms, correct_neg)。"""
    hits = sum(1 for p, o in zip(pred_pos, obs_pos) if p and o)
    misses = sum(1 for p, o in zip(pred_pos, obs_pos) if (not p) and o)
    fa = sum(1 for p, o in zip(pred_pos, obs_pos) if p and not o)
    cn = sum(1 for p, o in zip(pred_pos, obs_pos) if (not p) and not o)
    pod = hits / (hits + misses) if hits + misses else None
    pofd = fa / (fa + cn) if fa + cn else None
    if pod is None or pofd is None:
        return None, pod, pofd, hits, misses, fa, cn
    return pod - pofd, pod, pofd, hits, misses, fa, cn


def pss_max(scores, labels):
    """スコア (大きいほど晴れ) の全候補閾値で PSS を計算し、最大値とその閾値を返す。"""
    pairs = sorted(((s, l) for s, l in zip(scores, labels) if s is not None), key=lambda t: -t[0])
    n1 = sum(1 for _, l in pairs if l)
    n0 = len(pairs) - n1
    if n1 == 0 or n0 == 0:
        return None, None
    best, best_th = -1.0, None
    hits = fa = 0
    i = 0
    while i < len(pairs):
        th = pairs[i][0]
        # 同点はまとめて「閾値以上 = 晴れ予報」に入れる
        while i < len(pairs) and pairs[i][0] == th:
            if pairs[i][1]:
                hits += 1
            else:
                fa += 1
            i += 1
        val = hits / n1 - fa / n0
        if val > best:
            best, best_th = val, th
    return best, best_th


def _delong_components(scores, labels):
    """DeLong の構造成分 (V10: 陽性ごと, V01: 陰性ごと) と AUC。auc() と同じ計算を分けて返す。"""
    import bisect
    pos = [s for s, l in zip(scores, labels) if l]
    neg = [s for s, l in zip(scores, labels) if not l]
    n1, n0 = len(pos), len(neg)
    neg_sorted, pos_sorted = sorted(neg), sorted(pos)
    v10 = []
    for s in pos:
        lo, hi = bisect.bisect_left(neg_sorted, s), bisect.bisect_right(neg_sorted, s)
        v10.append((lo + 0.5 * (hi - lo)) / n0)
    v01 = []
    for s in neg:
        lo, hi = bisect.bisect_left(pos_sorted, s), bisect.bisect_right(pos_sorted, s)
        v01.append(((n1 - hi) + 0.5 * (hi - lo)) / n1)
    return sum(v10) / n1, v10, v01


def delong_paired_test(scores_a, scores_b, labels):
    """同一標本・同一ラベルに対する 2 つのスコアの AUC 差の DeLong 検定 (DeLong, DeLong & Clarke-Pearson 1988)。
    scores_a/b は同じ順序 (同じ事例) で与える。返り値 dict: auc_a, auc_b, diff, se, z, p (両側, 正規近似), n_pos, n_neg。"""
    pairs = [(a, b, l) for a, b, l in zip(scores_a, scores_b, labels) if a is not None and b is not None]
    sa, sb, lab = [p[0] for p in pairs], [p[1] for p in pairs], [p[2] for p in pairs]
    n1, n0 = sum(1 for l in lab if l), sum(1 for l in lab if not l)
    if n1 < 2 or n0 < 2:
        return None
    auc_a, v10a, v01a = _delong_components(sa, lab)
    auc_b, v10b, v01b = _delong_components(sb, lab)

    def cov(x, y, mx, my):
        return sum((xi - mx) * (yi - my) for xi, yi in zip(x, y)) / (len(x) - 1)
    s10 = [[cov(v10a, v10a, auc_a, auc_a), cov(v10a, v10b, auc_a, auc_b)],
           [cov(v10b, v10a, auc_b, auc_a), cov(v10b, v10b, auc_b, auc_b)]]
    s01 = [[cov(v01a, v01a, auc_a, auc_a), cov(v01a, v01b, auc_a, auc_b)],
           [cov(v01b, v01a, auc_b, auc_a), cov(v01b, v01b, auc_b, auc_b)]]
    # Var(auc_a - auc_b) = c' (S10/n1 + S01/n0) c, c = (1, -1)
    var = (s10[0][0] - 2 * s10[0][1] + s10[1][1]) / n1 + (s01[0][0] - 2 * s01[0][1] + s01[1][1]) / n0
    diff = auc_a - auc_b
    se = math.sqrt(var) if var > 0 else 0.0
    z = diff / se if se > 0 else (0.0 if diff == 0 else float("inf"))
    p = math.erfc(abs(z) / math.sqrt(2)) if se > 0 else (1.0 if diff == 0 else 0.0)
    return {"auc_a": auc_a, "auc_b": auc_b, "diff": diff, "se": se, "z": z, "p": p, "n_pos": n1, "n_neg": n0}
