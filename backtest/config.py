"""Phase 1 の固定設定: 地点・モデル・変数・リードタイム・期間・パス。
根拠は docs/design.md §3-4 と docs/api-findings.md。"""
from datetime import date, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
RAW_DIR = DATA_DIR / "raw" / "previous_runs"
PARQUET_PATH = DATA_DIR / "parquet" / "forecast_long.parquet"

PREVIOUS_RUNS_URL = "https://previous-runs-api.open-meteo.com/v1/forecast"

# design.md §4。座標は本体 MOUNTAINS (読み取りのみ) の値。
SITES = [
    {"site_id": "karamatsu", "name": "唐松岳", "lat": 36.7595, "lon": 137.7503, "elevation_m": 2696,
     "station_id": "48141", "station_name": "白馬"},
    {"site_id": "fuji", "name": "富士山(剣ヶ峰)", "lat": 35.3606, "lon": 138.7274, "elevation_m": 3776,
     "station_id": "50066", "station_name": "富士山"},
    {"site_id": "akadake", "name": "八ヶ岳(赤岳)", "lat": 35.9722, "lon": 138.3672, "elevation_m": 2899,
     "station_id": "48571", "station_name": "野辺山"},
    {"site_id": "nikko_shirane", "name": "日光白根山", "lat": 36.7983, "lon": 139.3742, "elevation_m": 2578,
     "station_id": "41166", "station_name": "奥日光"},
    # 八方池は ECMWF 0.25° で唐松岳と同一格子だったため外し (Phase 1 レビュー)、
    # 代わりに最寄りフル要素観測所との距離・標高差が全76座で最小の安達太良山を入れた。
    {"site_id": "adatara", "name": "安達太良山", "lat": 37.6256, "lon": 140.2864, "elevation_m": 1700,
     "station_id": "36196", "station_name": "鷲倉"},
]

# Phase 2 で日次収集するアメダス観測所。検証地点のペア + 高標高の参考地点。
AMEDAS_STATIONS = {
    "48141": "白馬", "50066": "富士山", "48571": "野辺山", "41166": "奥日光", "36196": "鷲倉",
    "48216": "菅平", "42221": "田代", "42121": "草津", "48531": "開田高原", "48466": "奈川",
    "48331": "軽井沢", "49256": "山中", "48156": "長野",
}
AMEDAS_ELEMENTS = ["sun10m", "sun1h", "precipitation1h", "temp", "wind", "windDirection", "humidity", "pressure"]  # pressure: 官署と富士山のみ (正時のみ値あり)

MODELS = ["jma_msm", "ecmwf_ifs025"]

# api-findings §1.1: cloud_cover_low/mid/high と wind_gusts_10m の previous_dayN は
# 3モデルとも全null。それでも要求どおり全て投げ、欠損率としてデータ側に記録する。
VARIABLES = [
    "cloud_cover", "cloud_cover_low", "cloud_cover_mid", "cloud_cover_high",
    "precipitation", "temperature_2m", "wind_speed_10m", "wind_gusts_10m",
    "relative_humidity_2m",
]
LEAD_DAYS = list(range(1, 8))
# jma_msm は day3+ が全null (MSM の予報時間の仕様、api-findings §1.1)。加えて 9変数×7リード=63列の
# 月チャンクは Open-Meteo 側のストリーミングタイムアウト ("timeoutReached"、HTTP 200 非JSON) に
# 繰り返し当たったため (2026-09-18 実測)、jma_msm はリード 1-2 だけ要求する。
LEAD_DAYS_BY_MODEL = {"jma_msm": [1, 2], "ecmwf_ifs025": LEAD_DAYS}


def leads_for(model: str) -> list[int]:
    return LEAD_DAYS_BY_MODEL.get(model, LEAD_DAYS)

# 風速は m/s で受け取る (Open-Meteo の既定は km/h)。
REQUEST_UNITS = {"wind_speed_unit": "ms", "timezone": "UTC"}

SLEEP_SECONDS = 1.5
REQUEST_TIMEOUT = 120
MAX_RETRIES = 5


def default_period(today: date = None) -> tuple[date, date]:
    """過去12ヶ月: 昨日を終端に、その364日前を始端にする。"""
    today = today or date.today()
    end = today - timedelta(days=1)
    start = end - timedelta(days=364)
    return start, end


def hourly_param(variables=None, leads=None) -> str:
    variables = variables or VARIABLES
    leads = leads or LEAD_DAYS
    return ",".join(f"{v}_previous_day{n}" for v in variables for n in leads)
