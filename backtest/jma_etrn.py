"""気象庁「過去の気象データ検索」(etrn) の官署 (s1) 時別値を取得して JSON に保存する。
富士山 (prec_no=50, block_no=47639) の日照時間が夏季 (7〜8 月頃) だけ入っている (api-findings §10.5) ので、
山頂で唯一の晴天実況として使う。HTML スクレイピング (API は無い) なので 1 日 1 リクエスト、2 秒間隔、取得済みはスキップ。

    python -m backtest.jma_etrn --station fuji --start 2026-07-01 --end 2026-09-17

保存: data/obs/etrn/<station>/<YYYY-MM-DD>.json (パース済み), data/obs/etrn/<station>/html/<YYYY-MM-DD>.html.gz (生)
列の対応は probe/log_fuji_etrn.txt で実測したヘッダ順 (hourly_s1):
  時 | 気圧(現地, 海面) | 降水量 | 気温 | 露点温度 | 蒸気圧 | 湿度 | 風向・風速(風速, 風向) | 日照時間 | 全天日射量 | 雪(降雪, 積雪) | 天気 | 雲量 | 視程
"""
import argparse
import gzip
import json
import logging
import re
import time
from datetime import date, timedelta
from pathlib import Path

import requests

from . import config

log = logging.getLogger(__name__)

ETRN_DIR = config.DATA_DIR / "obs" / "etrn"
URL = "https://www.data.jma.go.jp/stats/etrn/view/hourly_s1.php?prec_no={prec}&block_no={block}&year={y}&month={m}&day={d}&view="
HEADERS = {"User-Agent": "yamabiyori-backtest etrn fetcher (personal research, 1 page/day, 2s interval)"}
STATIONS = {
    # key: (prec_no, block_no, アメダス地点番号, 名称)
    "fuji": (50, 47639, "50066", "富士山"),
}
S1_COLUMNS = ["hour", "pressure", "sea_level_pressure", "precipitation1h", "temp", "dewpoint", "vapor_pressure",
              "humidity", "wind", "windDirection", "sun1h", "solar_mj", "snowfall", "snowdepth", "weather", "cloud_amount", "visibility"]
NUMERIC = {"pressure", "sea_level_pressure", "precipitation1h", "temp", "dewpoint", "vapor_pressure", "humidity",
           "wind", "sun1h", "solar_mj", "snowfall", "snowdepth", "cloud_amount", "visibility"}


def parse_value(text: str):
    """'12.3' -> 12.3, '0.5 )' (準正常) -> 0.5, '×'/'--'/'' (欠測・観測なし) -> None。"""
    t = text.replace(")", "").replace("]", "").strip()
    if t in ("", "×", "--", "///", "#"):
        return None
    try:
        return float(t)
    except ValueError:
        return t


def parse_s1(html: str) -> list[dict]:
    rows = []
    for row in re.findall(r"<tr class=\"mtx\"[^>]*>(.*?)</tr>", html, re.S):
        cells = [re.sub(r"<.*?>", "", c).strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        if len(cells) < len(S1_COLUMNS) or not cells[0].isdigit():
            continue
        rec = {}
        for name, raw in zip(S1_COLUMNS, cells):
            rec[name] = int(raw) if name == "hour" else (parse_value(raw) if name in NUMERIC else (raw or None))
        rows.append(rec)
    return rows


def fetch_day(session: requests.Session, station: str, d: date, out_dir: Path = ETRN_DIR) -> Path | None:
    prec, block, sid, _ = STATIONS[station]
    json_path = out_dir / station / f"{d:%Y-%m-%d}.json"
    if json_path.exists():
        return None
    url = URL.format(prec=prec, block=block, y=d.year, m=d.month, d=d.day)
    r = session.get(url, headers=HEADERS, timeout=60)
    r.raise_for_status()
    r.encoding = "utf-8"
    html_dir = out_dir / station / "html"
    html_dir.mkdir(parents=True, exist_ok=True)
    with gzip.open(html_dir / f"{d:%Y-%m-%d}.html.gz", "wt", encoding="utf-8") as f:
        f.write(r.text)
    rows = parse_s1(r.text)
    if len(rows) != 24:
        log.warning("%s %s: parsed %d rows (expected 24)", station, d, len(rows))
    json_path.write_text(json.dumps({"station": station, "station_id": sid, "date_jst": d.isoformat(), "url": url,
                                     "rows": rows}, ensure_ascii=False), encoding="utf-8")
    return json_path


def fetch_range(station: str, start: date, end: date, out_dir: Path = ETRN_DIR, sleep_seconds: float = 2.0) -> int:
    session = requests.Session()
    n = 0
    d = start
    while d <= end:
        p = fetch_day(session, station, d, out_dir)
        if p is not None:
            n += 1
            log.info("fetched %s %s", station, d)
            time.sleep(sleep_seconds)
        d += timedelta(days=1)
    return n


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--station", default="fuji", choices=list(STATIONS))
    ap.add_argument("--start", type=date.fromisoformat, required=True)
    ap.add_argument("--end", type=date.fromisoformat, required=True)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    n = fetch_range(args.station, args.start, args.end)
    print(f"fetched {n} day(s)")


if __name__ == "__main__":
    main()
