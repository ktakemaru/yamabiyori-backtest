# R12 の本体反映 (v1.6.0) の検証記録

作成 2026-09-23。本体の作業ブランチ `feature/r12-msm7` (worktree `C:\mountain-weather-r12`、origin/main = v1.5.0 `52d3d2a` から分岐) に対する確認。
計画は [r12-plugin-integration-plan.md](r12-plugin-integration-plan.md) (案 A: 本体の正規化を行ごとの基準値に最小限で直す)。

| コミット | 内容 |
|---|---|
| `43557ea` | `mountain_weather_core.py` だけの変更 (MSM d1-2 行を新表の値に、基準値を各行の 5 番目の要素に、`CLOUD_CALIBRATION_P_REF` を廃止)。core の blob `51b38a6` |
| `5f7c5e5` | 文書のみ (plugin.json 1.6.0、CHANGELOG、README、SKILL.md、AGENTS.md)。コードの blob は `43557ea` と同じ (core `51b38a6`、detail `386fe91`、terrain `0890497` = v1.5.0 と同じ) |

## 1. 等価性 — 一致

`tests/test_mos_equivalence.py` の `v160` の 8 件 (本体 `43557ea` を `git show` で一時フォルダへ取り出して import):

- 1 値: モデル {ecmwf_ifs025, jma_msm, 未知モデル} × lead_day 1〜16 × 221 値で、`jma_msm` の lead_day 1〜2 は新表 (`mos_tables/r1-summit-cloud-sunny-msm7.json`) と、
  それ以外はすべて v1.5.0 (`52d3d2a`) と `==` (型も)。全範囲で新表 (mos) とも一致。
- 表: MSM d1-2 行だけが新表の値 (P_ref 0.822)、他の行と各行の P_ref は v1.5.0 と同じ。`CLOUD_CALIBRATION_P_REF` は無い。
- 系列: フィクスチャ 3 通り × 5 地点と合成系列 (today 3 通り × 山頂 3 標高) で、`calibrated_summit_cloud_series` が「本体の糊 + mos 新表」と一致し、
  MSM の lead_day 1〜2 以外の時刻は v1.5.0 と一致。

```
python -m pytest -q tests/test_mos_equivalence.py -k v160      # 8 passed
```

## 2. 前後比較 — 期待値と完全一致

`python -m backtest.r12_score_effect --body-commit 43557eacece2c4b4b70587e52401955808596cbf` → [r12-plugin-effect-2026-09-18-cache.txt](r12-plugin-effect-2026-09-18-cache.txt)。
本体のコードそのもの (v1.5.0 と v1.6.0) で 2026-09-18 の探索キャッシュ 1,140 山日を計算:

- v1.5.0 のスコア == 旧表を mos で差し替えた計算、v1.6.0 のスコア == 新表を mos で差し替えた計算、**山日ごとに完全一致 (不一致 0)**。
  つまり v1.6.0 − v1.5.0 は [r12-score-effect.txt](r12-score-effect.txt) の「新表 − 今の本体」と山日ごとに同じ。
- 1〜2 日目 152 山日: 変化 121、平均 +0.98 点、平均 |Δ| 2.96、|Δ| ≥ 5 点 11%、範囲 −7.4〜+7.5、**0 点をまたぐ変化 0 件**。3 日目以降 988 山日は変化 0。
- 変化量の区分 (R1 と同じ区分): +5〜+10 が 5、0〜+5 が 72、変化なし 31、−5〜0 が 32、−10〜−5 が 12。
- 探索モードの順位: 日別上位 10 座の入れ替わり 0 (全 15 日)、全 76 座の順位の Spearman は 1 日目 0.998・2 日目 0.996・3 日目以降 1.000。
  日ごとの平均スコアの前後差は 1 日目 +3.0、2 日目 −1.0 で、1〜2 日目が一律に上がってはいない。
- **参照日: 唐松岳 9/6 51.2 → 55.0、立山 9/5 91.8 → 95.6、槍ヶ岳 9/5 66.2 → 67.8 (期待値 55.0 / 95.6 / 67.8 と一致)**。
- 本体側 (`C:\mountain-weather` の `.git`・`cache`、`C:\mountain-weather-r8\cache_prev`) の更新時刻は実行前後で不変。

## 3. 確信度 — v1.5.0 と完全一致

`git archive` で `52d3d2a` と `43557ea` を一時フォルダに取り出し、それぞれを `--plugin-dir` にして `confidence_by_hash.recompute` を別プロセスで実行
(保存済みの `data/snapshots/plugin_confidence/` の入力を再計算、2026-09-18〜22 の 5 ファイル × 5 地点 = 230 行):

- **7 項目 (confidence / cloud_confidence / precip_confidence / temp_confidence / cloud_pct / precip_prob / precip_mm) がすべて完全一致** (行の並びも同じ)。
- 参考: v1.5.0 時代のスナップショット (hash 52d3d2a、190 行) は、v1.5.0 で再計算した値と保存値の差が 7 項目とも 0。

```
git -C C:/mountain-weather --no-optional-locks archive 52d3d2a | tar -x -C <tmp>/pv150
git -C C:/mountain-weather --no-optional-locks archive 43557ea | tar -x -C <tmp>/pv160
python -c "from backtest.confidence_by_hash import recompute; ..."   # pv150 / pv160 を別プロセスで、*_new 列を比較
```

確信度の計算 (`compute_ensemble_confidence_by_day`) は R1/R12 の較正の経路に入らないので、2027 年初の確信度の較正に使う系列は v1.6.0 で変わらない。
スナップショットの envelope の `plugin_version` / `git_hash` は v1.6.0 の公開後に変わるので、`confidence_by_hash summary` で版の切り替わりを確認すること。
