"""
天気予報取得モジュール。

- 情報源は2つ(いずれもキー不要・無料):
    Open-Meteo    ... 毎時の天気・降水確率・降水量・湿度・風。3日先まで無料で取れる。
    気象庁 公式JSON ... 日最高/最低気温。地点補正済みで正確。
  なぜ2つに分けているかは models.py の DayForecast 周辺の長いコメントを参照。
  要約すると、Open-Meteo の格子モデルは京都盆地の日中高温を過小評価する
  (実測で気象庁より 3〜5℃ 低い)ため、日最高/最低だけは気象庁の値を信頼し、
  毎時の値は「形は合っているが絶対値は参考値」として別扱いにしている。

- tenki.jp のような「1〜24時」表記に合わせるため、Open-Meteo には
  config の days + 1 日ぶんを要求する(24時 = 翌日00時のデータが要るため)。
  時刻の対応付けは hourly.time の文字列と日付・時刻を突き合わせて行い、
  配列添字の算術(d*24+h 等)には頼らない。Open-Meteo が返す配列の先頭が
  必ずしも当日00時とは限らず、ずれると全時刻が狂うため。

- 気象庁の JSON は [0](直近3日の詳細予報)と [1](週間予報)からなり、
  系列の並び順は発表時刻によって変わるため、添字を固定せず
  "temps" / "tempsMin","tempsMax" キーの有無で目的の系列を探す。
  [0] のほうが精度が高いため、日付が重複する場合は [0] を優先する。

- このモジュールの公開関数はいずれも例外を投げない。天気の取得に失敗しても
  ニュースの通知自体は止めたくないため、失敗時は None / {} / [] を返して
  ログに残すだけにとどめる(hatena_count.py と同じ方針)。
"""

from datetime import date as date_type
from datetime import datetime, timedelta, timezone

import requests

from models import DayForecast, HourPoint

JST = timezone(timedelta(hours=9))
WEEKDAY_JA = ("月", "火", "水", "木", "金", "土", "日")

USER_AGENT = "news-bot/1.0"
REQUEST_TIMEOUT_SEC = 20

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
JMA_FORECAST_URL = "https://www.jma.go.jp/bosai/forecast/data/forecast/{code}.json"

# WMO weather code -> (絵文字, 和名)。Open-Meteo の weather_code はこの表に従う。
WMO_CODES = {
    0: ("☀️", "晴れ"), 1: ("🌤️", "晴れ"), 2: ("⛅", "薄曇り"), 3: ("☁️", "曇り"),
    45: ("🌫️", "霧"), 48: ("🌫️", "霧"),
    51: ("🌦️", "小雨"), 53: ("🌦️", "小雨"), 55: ("🌧️", "雨"),
    56: ("🌧️", "凍雨"), 57: ("🌧️", "凍雨"),
    61: ("🌦️", "小雨"), 63: ("🌧️", "雨"), 65: ("🌧️", "強い雨"),
    66: ("🌧️", "凍雨"), 67: ("🌧️", "凍雨"),
    71: ("🌨️", "小雪"), 73: ("🌨️", "雪"), 75: ("❄️", "大雪"), 77: ("🌨️", "細氷"),
    80: ("🌦️", "にわか雨"), 81: ("🌧️", "にわか雨"), 82: ("⛈️", "激しい雨"),
    85: ("🌨️", "にわか雪"), 86: ("❄️", "にわか雪"),
    95: ("⛈️", "雷雨"), 96: ("⛈️", "雷雨"), 99: ("⛈️", "雷雨"),
}
_UNKNOWN = ("❓", "不明")

# 風向(度)を8方位に丸めたときの和名。0度(北)から時計回りに45度刻み。
_WIND_NAMES = ("北", "北東", "東", "南東", "南", "南西", "西", "北西")
# 上と同じ並びに対応する矢印。矢印は「風が向かう先」(= 風向 + 180度)を指す。
# 例えば北風(0度)は南に向かって吹くので、_WIND_NAMES[0]="北" に対応する矢印は
# 南向きの ⬇️ になる。
_WIND_ARROWS = ("⬇️", "↙️", "⬅️", "↖️", "⬆️", "↗️", "➡️", "↘️")


def weather_mark(code: int) -> str:
    """WMO weather code を絵文字にする。未知のコードは "❓"。"""
    return WMO_CODES.get(code, _UNKNOWN)[0]


def weather_name(code: int) -> str:
    """WMO weather code を和名にする。未知のコードは "不明"。"""
    return WMO_CODES.get(code, _UNKNOWN)[1]


def _wind_index(degrees: int) -> int:
    """風向(度)を8方位のインデックス(0=北, 1=北東, ...)に丸める。"""
    return round((degrees % 360) / 45) % 8


def wind_arrow(degrees: int) -> str:
    """風向を「風が向かう先」を指す矢印絵文字にする。"""
    return _WIND_ARROWS[_wind_index(degrees)]


def wind_name(degrees: int) -> str:
    """風向を8方位の日本語("北東" 等)にする。"""
    return _WIND_NAMES[_wind_index(degrees)]


def fetch_open_meteo(weather_cfg: dict) -> dict | None:
    """
    Open-Meteo から毎時予報と日の出/日の入りを取得する。

    forecast_days は config の days + 1 を渡す。tenki.jp 流の「24時」表記
    (=翌日00時)を埋めるのに、指定日数の翌日ぶんのデータが1点だけ要るため。

    通信・HTTP エラー時は例外を投げず None を返し、ログに残す。
    気象庁側が生きていても毎時データが無ければ表そのものが作れないため、
    呼び出し側(build_forecasts)はこれが None なら処理を打ち切る。
    """
    days = weather_cfg.get("days", 2)
    params = {
        "latitude": weather_cfg.get("latitude"),
        "longitude": weather_cfg.get("longitude"),
        "hourly": (
            "temperature_2m,precipitation_probability,precipitation,"
            "relative_humidity_2m,weather_code,wind_speed_10m,wind_direction_10m"
        ),
        "daily": "sunrise,sunset",
        "timezone": "Asia/Tokyo",
        "forecast_days": days + 1,
        # 既定は km/h。HourPoint.wind_speed は m/s の契約なのでここで揃える。
        "wind_speed_unit": "ms",
    }
    try:
        resp = requests.get(
            OPEN_METEO_URL,
            params=params,
            headers={"User-Agent": USER_AGENT},
            timeout=REQUEST_TIMEOUT_SEC,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"[weather] Open-Meteo の取得に失敗しました: {e}")
        return None


def _find_series_with_key(time_series: list, key: str) -> dict | None:
    """timeSeries の中から、areas[0] に key を持つ系列を探す。

    気象庁 JSON は発表時刻によって系列の数や並び順が変わるため、
    添字を固定せずキーの有無で目的の系列を特定する。
    """
    for series in time_series:
        areas = series.get("areas") or []
        if areas and key in areas[0]:
            return series
    return None


def _find_area(series: dict, point_name: str) -> dict | None:
    """系列内から area.name が point_name と一致する area を探す。"""
    for area in series.get("areas", []):
        if area.get("area", {}).get("name") == point_name:
            return area
    return None


def fetch_jma_temps(weather_cfg: dict) -> dict[str, tuple[float, float]]:
    """
    気象庁 公式JSON から日ごとの (最低気温, 最高気温) を取得する。

    [0](直近3日の詳細予報)と [1](週間予報)の両方から気温を集めてマージする。
    [0] は "temps" 1本の系列に日々の観測・予報時刻の気温が並んでいるだけなので、
    日付ごとに min/max を取ることで日最低/最高とみなす。[1] は日ごとに
    tempsMin/tempsMax が別々に入っている。両方に同じ日付があれば [0] を優先する
    (短期予報のほうが精度が高いため)。

    通信・パース失敗時は例外を投げず空辞書を返し、ログに残す。気象庁が落ちていても
    Open-Meteo だけで通知は継続できる(毎時の気温が参考値のみになる)。
    """
    code = weather_cfg.get("jma_area_code", "260000")
    point_name = weather_cfg.get("jma_point_name", "京都")
    url = JMA_FORECAST_URL.format(code=code)

    try:
        resp = requests.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=REQUEST_TIMEOUT_SEC,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"[weather] 気象庁データの取得に失敗しました: {e}")
        return {}

    try:
        short_temps: dict[str, tuple[float, float]] = {}
        long_temps: dict[str, tuple[float, float]] = {}

        # [0] 直近3日の詳細予報: "temps" キーを持つ系列を探す
        if len(data) > 0:
            series = _find_series_with_key(data[0].get("timeSeries", []), "temps")
            if series is not None:
                area = _find_area(series, point_name)
                if area is not None:
                    time_defines = series.get("timeDefines", [])
                    temps = area.get("temps", [])
                    by_date: dict[str, list[float]] = {}
                    for t, v in zip(time_defines, temps):
                        if not v:
                            continue
                        try:
                            value = float(v)
                        except ValueError:
                            continue
                        by_date.setdefault(t[:10], []).append(value)
                    for d, values in by_date.items():
                        short_temps[d] = (min(values), max(values))

        # [1] 週間予報: "tempsMin"/"tempsMax" キーを持つ系列を探す
        if len(data) > 1:
            series = _find_series_with_key(data[1].get("timeSeries", []), "tempsMin")
            if series is not None:
                area = _find_area(series, point_name)
                if area is not None:
                    time_defines = series.get("timeDefines", [])
                    temps_min = area.get("tempsMin", [])
                    temps_max = area.get("tempsMax", [])
                    for t, lo, hi in zip(time_defines, temps_min, temps_max):
                        if not lo or not hi:
                            continue
                        try:
                            long_temps[t[:10]] = (float(lo), float(hi))
                        except ValueError:
                            continue

        # [0] を優先し、[0] に無い日付だけ [1] で埋める
        merged = dict(long_temps)
        merged.update(short_temps)
        return merged
    except Exception as e:
        print(f"[weather] 気象庁データの解析に失敗しました: {e}")
        return {}


def _safe_value(values: list, idx: int, default=0):
    """配列から idx 番目の値を取り出す。範囲外・None なら default を返す。

    Open-Meteo の hourly.precipitation_probability は null が混ざることがあり、
    毎時配列の長さが微妙に食い違うケースにも備える。
    """
    if idx < 0 or idx >= len(values):
        return default
    value = values[idx]
    return default if value is None else value


def build_forecasts(weather_cfg: dict, now: datetime | None = None) -> list[DayForecast]:
    """
    Open-Meteo と気象庁のデータを合成し、DayForecast のリストを作る。

    tenki.jp 流に1〜24時で1日を表現する(24時は翌日00時のデータを流用する)。
    時刻の対応付けは hourly.time の文字列と日付・時刻を突き合わせて行い、
    配列添字の算術には頼らない。

    この関数は例外を投げない。Open-Meteo が取れなければ表自体を作れないため
    空リストを返し、気象庁が取れない場合は official_low/high が None のまま
    続行する。
    """
    try:
        if now is None:
            now = datetime.now(JST)

        om = fetch_open_meteo(weather_cfg)
        if om is None:
            return []

        jma_temps = fetch_jma_temps(weather_cfg)

        hourly = om.get("hourly", {}) or {}
        times = hourly.get("time", []) or []
        time_index = {t: i for i, t in enumerate(times)}

        temperature = hourly.get("temperature_2m", []) or []
        pop = hourly.get("precipitation_probability", []) or []
        precipitation = hourly.get("precipitation", []) or []
        humidity = hourly.get("relative_humidity_2m", []) or []
        weather_code = hourly.get("weather_code", []) or []
        wind_speed = hourly.get("wind_speed_10m", []) or []
        wind_direction = hourly.get("wind_direction_10m", []) or []

        daily = om.get("daily", {}) or {}
        daily_times = daily.get("time", []) or []
        daily_index = {t: i for i, t in enumerate(daily_times)}
        sunrise_list = daily.get("sunrise", []) or []
        sunset_list = daily.get("sunset", []) or []

        forecasts: list[DayForecast] = []
        num_days = weather_cfg.get("days", 2)

        for d in range(num_days):
            target_date: date_type = now.date() + timedelta(days=d)

            hours: list[HourPoint] = []
            for h in range(1, 25):
                # tenki.jp 流: 24時は翌日00時のデータ
                if h == 24:
                    lookup_date = target_date + timedelta(days=1)
                    lookup_hour = 0
                else:
                    lookup_date = target_date
                    lookup_hour = h

                time_str = f"{lookup_date.isoformat()}T{lookup_hour:02d}:00"
                idx = time_index.get(time_str)
                if idx is None:
                    continue

                hours.append(
                    HourPoint(
                        hour=h,
                        weather_code=int(_safe_value(weather_code, idx, 0)),
                        temperature=float(_safe_value(temperature, idx, 0.0)),
                        pop=int(_safe_value(pop, idx, 0)),
                        precipitation=float(_safe_value(precipitation, idx, 0.0)),
                        humidity=int(_safe_value(humidity, idx, 0)),
                        wind_speed=float(_safe_value(wind_speed, idx, 0.0)),
                        wind_direction=int(_safe_value(wind_direction, idx, 0)),
                    )
                )

            if not hours:
                # この日のデータが1件も無ければ結果に含めない
                continue

            if d == 0:
                name = "今日"
            elif d == 1:
                name = "明日"
            elif d == 2:
                name = "明後日"
            else:
                name = ""
            weekday = WEEKDAY_JA[target_date.weekday()]
            date_part = f"{target_date:%m/%d}({weekday})"
            label = f"{name} {date_part}".strip()

            sunrise = None
            sunset = None
            d_idx = daily_index.get(target_date.isoformat())
            if d_idx is not None:
                if d_idx < len(sunrise_list) and sunrise_list[d_idx]:
                    sunrise = sunrise_list[d_idx][-5:]
                if d_idx < len(sunset_list) and sunset_list[d_idx]:
                    sunset = sunset_list[d_idx][-5:]

            official = jma_temps.get(target_date.isoformat())
            official_low, official_high = official if official else (None, None)

            forecasts.append(
                DayForecast(
                    date=target_date,
                    label=label,
                    hours=hours,
                    sunrise=sunrise,
                    sunset=sunset,
                    official_low=official_low,
                    official_high=official_high,
                )
            )

            if official_low is not None and official_high is not None:
                temps_str = f"気象庁 {official_low:.1f}〜{official_high:.1f}"
            else:
                temps_str = "気象庁データなし"
            print(f"[weather] {label}: {len(hours)}時間ぶん取得({temps_str}℃)")

        return forecasts
    except Exception as e:
        print(f"[weather] 予報の構築に失敗しました: {e}")
        return []
