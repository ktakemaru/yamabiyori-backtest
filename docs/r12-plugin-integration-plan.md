# R12 を本体に反映する計画 (v1.6.0 案) — 実装はしない

作成 2026-09-23。本体 (`C:\mountain-weather`, GitHub `ktakemaru/yamabiyori`) は読み取りのみで調べた (`git --no-optional-locks show 52d3d2a:...`)。
採用する表は **新表** `mos_tables/r1-summit-cloud-sunny-msm7.json` (table_set_version 1.1.0、content_sha256 `e4191f9b…`; 選んだ理由は
[r12-msm-transplant.md](r12-msm-transplant.md) §9)。

## 0. 前提 (確認済みの事実)

- 本体の公開版は v1.5.0 = `main` = `origin/main` = `52d3d2a` (2026-09-23 に `git ls-remote origin main` で確認)。**main への push がそのままマーケットプレイス公開**。
- 本体の主作業ツリー `C:\mountain-weather` は `feature/backtest-harness` を checkout 中で、未コミットの変更がある。`main` は worktree `C:\mountain-weather-r8` で checkout 中
  (untracked の `cache_prev/` 等あり。`cache_prev/` は R1・R12 の前後比較に使った 2026-09-18 のキャッシュ)。
- R1 の較正は `mountain_weather_core.py` だけで完結している。表と関数を参照するコードは core の中だけで (`git grep` で確認)、
  `mountain_weather_mvp.py` / `mountain_weather_detail.py` が import するのは `SUMMIT_CLOUD_CAL_VAR`・`calibrated_summit_cloud_series`・`CLOUD_CALIBRATION_ENABLED`・
  `CLOUD_CALIBRATION_FULL_SUMMIT_M`・`CLOUD_CALIBRATION_RAW_SUMMIT_M`。文章での言及は AGENTS.md:36、CHANGELOG.md:9、skills/yamabiyori/SKILL.md:122。
- 本体のスクリプトは `./venv/Scripts/python.exe mountain_weather_detail.py` のように直接起動される (SKILL.md:52, 531-535)。Python は起動したスクリプトのディレクトリを
  `sys.path` の先頭に入れるので、リポジトリ直下に置いたパッケージは import できる。
- 本体にはテストスイートが無い (ルートに tests/ が無い)。等価性はバックテスト側のテストで確かめる。
- 版番号の置き場所は `.claude-plugin/plugin.json` の `version` だけ (`marketplace.json` に version は無い)。v1.4.0 (87c9c3c) の変更ファイルは
  plugin.json・CHANGELOG・README・core・detail・scratch スクリプト・SKILL.md。

## 1. 何が問題か: 基準値 (P_ref) がモデル単位で決まる

v1.5.0 の正規化 (案C) は `CLOUD_CALIBRATION_P_REF = {r[0]: r[3][0] for r in CLOUD_CALIBRATION_TABLE if r[1] == 1}` (core:616) で、
**モデルごとに d1-2 行の 0% ビンを基準値にする**。`calibrated_cloud_pct` (core:637-644) はどの行でもこのモデル単位の値で割る。
そのため MSM の d1-2 行だけを新表の値 (0% ビン 0.719 → 0.822) に差し替えると、**d3-4 行の実効雲量も 0.822 で割られて変わる**
(0% で 18.6 → 28.8、(0,10] で 36.9 → 44.8 など 0.7〜10.2 ポイント辛くなる; [r12-msm-retrain.md](r12-msm-retrain.md) §2.3)。
新表では d3-4 行は旧基準値 0.719 のまま (v1.5.0 と同じ値) なので、本体でもこれを再現する必要がある。

## 2. 2 つの案

| | **A: 本体の正規化を行ごとの基準値に最小限で直す** | **B: 本体の R1 処理を mos モジュールに置き換える** |
|---|---|---|
| 変更するファイル | `mountain_weather_core.py` だけ (+ 文書) | `mountain_weather_core.py`、新規 `mos/` (4 ファイル)、新規 `mos_tables/r1-summit-cloud-sunny-msm7.json` (+ 文書) |
| 変更する箇所 | `CLOUD_CALIBRATION_TABLE` (core:607-613) の各行に基準値を 5 番目の要素として足し、MSM d1-2 行の値を新表に。`CLOUD_CALIBRATION_P_REF` (core:616) を廃止。`cloud_calibration_row` (core:628) を行全体を返す形に、`calibrated_cloud_pct` (core:637) で行の基準値を使う。バナー (core:520-599) の説明を更新 | `CLOUD_CALIBRATION_TABLE` / `_P_REF` / `cloud_calibration_bin` / `_row` / `calibrated_cloud_pct` を削除し、import 時に表を `json.load` → `mos.parse_table_set`。`calibrated_summit_cloud_series` (core:664) の中を「本体の糊 (モデル選択・暦日差の lead・標高ランプ) + `mos.calibrate(..., lead_hours=24*(lead_day-1), on_mismatch="allow")`」に。`CLOUD_CALIBRATION_ENABLED` / `FULL` / `RAW`・`cloud_calibration_weight`・`SUMMIT_CLOUD_CAL_VAR` は残す (mvp/detail が import) |
| 変更量の見込み | core で 30 行前後 (表 5 行、関数 10 行前後、バナー 15 行前後) | core で 40 行前後の削除・20〜30 行の追加、`mos/` 約 370 行、JSON 約 1,300 行 |
| 等価性テスト | バックテストの `test_mos_equivalence.py` に新しい本体コミットを固定して追加: 全モデル × lead_day 1〜16 × 221 値で、MSM の lead_day 1〜2 は新表 (mos) と、それ以外は 52d3d2a と `==` (型も) | A と同じテストに加え、本体に入れた `mos/` の各ファイルの sha256 がバックテストの固定コミットの `mos/` と一致すること、表の `content_sha256` が `e4191f9b…` と一致すること |
| 利点 | 変更が小さく、挙動が変わる箇所が MSM d1-2 行の値だけに限られる。import 時の I/O や新しい依存が増えない。レビューしやすい | 表の値がコードから JSON に移り、次の表の更新 (2027 年 3 月の作り直し) が「JSON の差し替え + 版の検査」で済む。行ごとの基準値・適用範囲 (`in_scope`)・lead の時間数での受け取りが最初からある (R11 の夜間・季節外の注記や lead の直しに使える)。hoshibiyori への移植の前段にもなる |
| 欠点 | 表の値は今後もコードに直書きのまま (バックテストの表との二重管理が続く)。R11 (夜間・季節外、lead の数え方) には何も効かない | 変更が大きく、一度に入る差分が多い。import 時に表ファイルを読むので、ファイルが欠けると本体が起動時に止まる (版の検査で気づける代わりに、壊れ方が大きい)。`mos/` をバックテストと本体の 2 か所で同じに保つ手間。AGENTS.md / SKILL.md の較正の説明をほぼ書き直す |
| 挙動の変化 | MSM の d1-2 行 (取得日から 1〜2 日目の MSM の時間) の実効雲量だけ | A と同じ (`on_mismatch="allow"` と暦日差の lead で v1.5.0 の糊を再現した場合) |

**推奨: v1.6.0 は A**。R12 の修正で変えたいのは MSM d1-2 行の値だけで、A なら挙動の変化がそこに限られ、差分も小さい。B の利点 (in_scope、lead の時間数、表の差し替え) が
効くのは R11 (夜間・季節外の扱い、lead の数え方) を直すときなので、**B は R11 の対応と一緒に別の版 (v1.7.0 以降) で行う** のがよい。

### 案 A の変更の形 (参考、コードは本体に書かない)

```python
# (model, lead_day_lo, lead_day_hi, P(sunny) per bin, P_ref)  -- P_ref は行ごと (R12, v1.6.0)
CLOUD_CALIBRATION_TABLE = [
    ("ecmwf_ifs025", 1, 2, [0.737, 0.492, 0.271, 0.206, 0.120, 0.052, 0.016, 0.000], 0.737),
    ("ecmwf_ifs025", 3, 5, [0.697, 0.485, 0.288, 0.222, 0.159, 0.115, 0.069, 0.069], 0.737),
    ("ecmwf_ifs025", 6, 10, [0.505, 0.415, 0.312, 0.312, 0.312, 0.257, 0.232, 0.129], 0.737),
    ("jma_msm", 1, 2, [0.822, 0.687, 0.517, 0.342, 0.260, 0.142, 0.062, 0.041], 0.822),   # 7 面で学び直し (R12)
    ("jma_msm", 3, 4, [0.585, 0.454, 0.377, 0.264, 0.238, 0.146, 0.123, 0.040], 0.719),   # 5 面学習のまま、基準値も v1.5.0 のまま
]
# cloud_calibration_row は行 (タプル) を返すようにし、calibrated_cloud_pct は行の 5 番目の要素 (P_ref) で割る。
# 未知モデル → ecmwf の行 (その行の P_ref 0.737) になるので v1.5.0 の挙動と同じ。
```

## 3. d1-2 行以外が v1.5.0 と完全に一致することの確認 (どちらの案でも)

1. 本体の作業ブランチ (§6) にコミットしたら、そのコミット hash をバックテストの `tests/test_mos_equivalence.py` に定数で追加し (`BODY_COMMIT_V160`)、
   既存の 52d3d2a と同じ方法 (`git show` で一時フォルダへ、`--no-optional-locks`、コミットが無ければ skip) で読み込む。
2. 1 値の比較: モデル {ecmwf_ifs025, jma_msm, 未知モデル} × lead_day 1〜16 × 値 221 通り (境界・負値・100 超・None・NaN・0.5 刻み) で、
   - `jma_msm` の lead_day 1〜2 → v1.6.0 の `calibrated_cloud_pct` == `mos.calibrate(新表)` (型も)
   - **それ以外すべて → v1.6.0 == v1.5.0 (52d3d2a)** (型も)。未知モデル・lead の範囲外 (nearest) も含む。
3. 系列の比較: 既存のフィクスチャ (Single Runs 5 地点、MSM の有無 3 通り) と合成系列 (月・日をまたぎ None・NaN 入り、today 3 通り × 山頂 3 標高) で、
   `calibrated_summit_cloud_series` の v1.6.0 と「本体の糊 + mos 新表」が一致し、MSM の lead_day 1〜2 以外の時刻は v1.5.0 と一致すること。
4. 案 B では追加で、本体の `mos/` の各ファイルの sha256 と表の `content_sha256` が期待値と一致すること (移植先の版の固定テスト; mos-module-design.md §2.5)。

## 4. 前後比較の手順 (R1・R8 のときと同じ)

1. **探索キャッシュでの日スコア**: `python -m backtest.r12_score_effect` を、本体の新しいコミットを直接読む形に拡張して回す (`--body-commit <v1.6.0 の hash>`;
   表の差し替えではなく v1.6.0 のコードそのもので計算)。キャッシュは R1・R12 と同じ 2026-09-18 のもの (`C:/mountain-weather-r8/cache_prev/`) と、反映直前に取った新しいもの 1 つ。
   - 2026-09-18 のキャッシュでは、**v1.6.0 − v1.5.0 は既に測った「新表 − 今の本体」と完全に一致するはず** (1〜2 日目 152 山日で平均 +0.98 点、|Δ| ≥ 5 点 11%、
     3 日目以降は変化 0、0 点をまたぐ変化 0 件; r12-msm-transplant.md §7)。一致しなければ実装の誤り。
   - R1 の記録 (`docs/r1-plugin-effect-2026-09-18-cache.txt`) と同じ形で、変化量の区分 (<−20 / −20〜−10 / −10〜−5 / −5〜0 / 0〜+5 / +5〜+10 / >+10) と、
     **0 点から動いた山日・0 点へ落ちた山日を別枠** で数える。標高帯別 (<1,500 / 1,500〜2,999 / ≥3,000m) と lead 別 (1〜2 日目 / 3 日目以降) も出す。
2. **探索モードの順位**: R1 で見たのと同じく、日ごとの上位 10 座の入れ替わりと、日ごとの順位の Spearman 相関 (前後) を出す。1〜2 日目だけが動くので、
   「1〜2 日目の山が 3 日目以降と比べて一律に上下していないか」を日ごとの平均スコアの前後差で確認する (R12 の §9 の理由 2 の確認)。
3. **参照日**: 唐松岳 9/6 (80 未満であるべき)、立山 9/5、槍ヶ岳 9/5。期待値は新表での計算 55.0 / 95.6 / 67.8 (v1.5.0 は 51.2 / 91.8 / 66.2)。
4. 結果はバックテストの `docs/r12-plugin-effect-<キャッシュの日付>-cache.txt` に保存し、本体の CHANGELOG の v1.6.0 の項に要点を書く (R1・R8 と同じ)。

## 5. 本体のドキュメントに載せる既知の制約 (案)

README の「精度と限界」と SKILL.md の較正の節に、次の 3 点を足す (文案):

- **冬は曇り予報が悲観的に出る**: 「雲量の較正表は 6〜9 月 (暖候期) の日中のデータで作っています。冬 (10〜5 月) にも同じ表を使っていて、短いリードでの暫定検証では、
  山頂に雲がかかる予報の時間 (較正後の晴れ確率 0.12 以下) でも麓では 13〜43% の時間が晴れていました。冬は、山頂に雲がかかる予報の日のスコアが低めに出る可能性があります
  (晴れ予報の日は影響を受けません)。麓の日照で確かめているため、山頂だけ雲の中という冬型の日にどこまで当てはまるかは未確認です。」
  (出典: バックテスト [r1-winter-provisional.md](r1-winter-provisional.md) §3.4)
- **MSM の 3〜4 日目は旧式の較正のまま**: 「取得日から 3〜4 日目の MSM の時間 (1 回の予報あたり平均 29 時間) は、較正表の入力 (山頂雲量の作り方) が
  本体と違う旧版のままです (900/800hPa を使わずに学習)。2027 年 3 月に作り直す予定です。」(出典: product-recommendations R12)
- **冬の 3 日目以降は未検証**: 「冬の較正の検証は、短いリード (1〜2 日目に相当) についての暫定的なものだけです。冬の 3 日目以降の予報について、較正が良いか悪いかは確かめていません。」
  (出典: r1-winter-provisional.md 冒頭の結論の範囲)

## 6. 版番号と作業手順

**版番号: v1.6.0** (スコアの計算が変わる = マイナー版。v1.4.0 (R8)・v1.5.0 (R1) と同じ扱い)。

変更するファイル (案 A): `mountain_weather_core.py`、`.claude-plugin/plugin.json` (`"version": "1.6.0"`)、`CHANGELOG.md` (v1.6.0 の項: 変更点・理由・前後比較の要点・
バックテストの文書への参照)、`README.md` (精度と限界・履歴)、`skills/yamabiyori/SKILL.md` (:122 付近の較正の中身、§5 の既知の制約)、`AGENTS.md` (:36)、
前後比較用の scratch スクリプト (R1・R8 と同じく本体に残すなら `scratch_r12_effect.py`)。

**worktree で作業する手順** (本体の `.git` に書き込むので、バックテスト側の作業ではなく、本体の作業として実施する):

```powershell
cd C:\mountain-weather
git --no-optional-locks status          # 主作業ツリー (feature/backtest-harness, 未コミットあり) は触らない
git fetch origin
# main は C:\mountain-weather-r8 で checkout 中なので、新しいブランチで別の worktree を作る
git worktree add ..\mountain-weather-r12 -b r12-msm7-v160 origin/main
cd ..\mountain-weather-r12
# 案 A の変更 → コミット (まだ push しない)
```

- worktree には `venv/` と `cache/` が無い (.gitignore 対象)。実行が必要なら主作業ツリーの Python を使う (`C:\mountain-weather\venv\Scripts\python.exe mountain_weather_mvp.py`;
  スクリプトのディレクトリが `sys.path` の先頭に入るので worktree 側のコードが読まれる)。その場合 `cache/` は worktree の中に新しく作られる (追跡対象外)。
- 等価性 (§3) と前後比較 (§4) はバックテスト側から、作業ブランチのコミットを `git show` で読んで行う (worktree での実行は不要)。
- 確認が済んだら、`r12-msm7-v160` を GitHub に push してプルリクエストで `main` へ入れる (**main への反映 = 公開** なので、ローカルで main に merge して push するより、
  レビューの段階を挟める)。公開後に `git ls-remote origin main` で hash を確認し、バックテストの等価性テストの固定コミットを必要に応じて更新する。
- 最後に `git worktree remove ..\mountain-weather-r12` (r8 の worktree と untracked の `cache_prev/` は触らない)。

**確信度スナップショット (2027 年初の較正対象) への影響**: R12 は山頂雲量の較正だけを変え、確信度 (`compute_ensemble_confidence_by_day`、アンサンブル由来) の計算には触れない
(R1 のときと同じ)。ただしスナップショットの envelope の `plugin_version` / `git_hash` は v1.6.0 に変わるので、`confidence_by_hash summary` で版の切り替わりを記録しておく。
