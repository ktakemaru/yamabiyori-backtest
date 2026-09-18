"""本体 (mountain_weather_core.py L293-339, 2026-09-18 時点) の標高内挿ロジックの移植。

本体は Open-Meteo の旧変数名 (`cloudcover_850hPa`, `geopotential_height_850hPa`) で hourly dict を持つ。
Single Runs API の現行名は `cloud_cover_850hPa` なので、ここでは「kind → 変数名」を関数で差し替えられる
ようにしてある (`level_var`)。ロジック自体は本体と同じ:
  - 各時刻、(geopotential_height, 値) が両方ある気圧面だけで高さ昇順の profile を作る
  - 目標高度が profile の下端以下なら下端の値、上端以上なら上端の値 (クランプ)
  - 間なら高さで線形内挿。winddirection は近い側の面の値
  - profile が空なら None
tests/test_summit_interp.py で本体の実装 (読み取りのみ import) と同一入力・同一出力を確認している。"""

LEVEL_STACK_HPA = [1000, 925, 900, 850, 800, 700, 600]   # 本体 core.LEVEL_STACK_HPA と同じ
GPH_KIND = "geopotential_height"

# Single Runs / Forecast API の現行変数名 -> 本体の kind 名
KIND_ALIASES = {"cloud_cover": "cloudcover", "wind_speed": "windspeed", "wind_direction": "winddirection"}


def level_var(kind: str, hpa: int) -> str:
    return f"{kind}_{hpa}hPa"


def level_profile(hourly: dict, kind: str, idx: int, levels=None) -> list:
    pts = []
    for hpa in levels or LEVEL_STACK_HPA:
        z = hourly.get(level_var(GPH_KIND, hpa))
        v = hourly.get(level_var(kind, hpa))
        if z and v and idx < len(z) and idx < len(v) and z[idx] is not None and v[idx] is not None:
            pts.append((z[idx], v[idx]))
    pts.sort()
    return pts


def interp_at_altitude(hourly: dict, kind: str, idx: int, target_m: float, levels=None):
    pts = level_profile(hourly, kind, idx, levels)
    if not pts:
        return None
    if target_m <= pts[0][0]:
        return pts[0][1]
    if target_m >= pts[-1][0]:
        return pts[-1][1]
    for (z0, v0), (z1, v1) in zip(pts, pts[1:]):
        if z0 <= target_m <= z1:
            if kind in ("winddirection", "wind_direction"):
                return v0 if target_m - z0 <= z1 - target_m else v1
            if z1 == z0:
                return v0
            return v0 + (v1 - v0) * (target_m - z0) / (z1 - z0)
    return None


def series_at_altitude(hourly: dict, kind: str, target_m: float, levels=None) -> list:
    """add_altitude_columns() の 1 列分: 全時刻の内挿値。"""
    n = len(hourly["time"])
    return [interp_at_altitude(hourly, kind, i, target_m, levels) for i in range(n)]


def to_core_naming(hourly: dict) -> dict:
    """Single Runs の hourly (`cloud_cover_850hPa` …) を本体の命名 (`cloudcover_850hPa` …) に写す。
    本体実装との突き合わせテストと、本体と同じ関数名で呼ぶための補助。"""
    out = {}
    for k, v in hourly.items():
        for new, old in KIND_ALIASES.items():
            if k.startswith(new + "_"):
                k = old + k[len(new):]
                break
        out[k] = v
    return out
