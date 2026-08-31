"""weather.py(天気予報取得モジュール)と notify_discord.py の天気予報部分のテスト。

ネットワークには一切アクセスしない。requests.get は monkeypatch で差し替え、
気象庁 JSON / Open-Meteo JSON の実データ形状を模したデータを直接与える。
"""

from datetime import date, datetime, timedelta, timezone

import pytest

import weather
from models import DayForecast, HourPoint
from notify_discord import build_weather_embed, format_day_forecast, notify_weather
from weather import (
    build_forecasts,
    fetch_jma_temps,
    fetch_open_meteo,
    wind_arrow,
    wind_name,
    weather_mark,
    weather_name,
)

JST = timezone(timedelta(hours=9))


# =========================================================
# weather_mark / weather_name
# =========================================================

@pytest.mark.parametrize(
    "code,mark,name",
    [
        (0, "☀️", "晴れ"),
        (3, "☁️", "曇り"),
        (51, "🌦️", "小雨"),
        (95, "⛈️", "雷雨"),
    ],
)
def test_weather_mark_nameは既知のWMOコードを絵文字と和名に変換する(code, mark, name):
    assert weather_mark(code) == mark
    assert weather_name(code) == name


def test_weather_mark_nameは未知のコードで不明を返す():
    assert weather_mark(999) == "❓"
    assert weather_name(999) == "不明"


# =========================================================
# wind_arrow / wind_name
# =========================================================

def test_wind_arrow_nameは0度で北と風が向かう先の下向き矢印になる():
    assert wind_name(0) == "北"
    assert wind_arrow(0) == "⬇️"


def test_wind_arrow_nameは90度で東と左向き矢印になる():
    assert wind_name(90) == "東"
    assert wind_arrow(90) == "⬅️"


def test_wind_arrow_nameは360度を0度と同じ扱いにする():
    assert wind_name(360) == wind_name(0)
    assert wind_arrow(360) == wind_arrow(0)


def test_wind_indexは22_5度刻みの境界で正しく丸める():
    # 22度は北寄り、23度は北東寄りに丸まる(北東の中心は45度)。
    assert wind_name(22) == "北"
    assert wind_name(23) == "北東"
    # 337度は北西寄り、338度は北(360度=0度)寄りに丸まる(0度をまたぐ境界)。
    assert wind_name(337) == "北西"
    assert wind_name(338) == "北"


# =========================================================
# fetch_jma_temps
# =========================================================

def _jma_response(monkeypatch, data):
    """requests.get を monkeypatch し、data を JSON として返すようにする。"""

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return data

    def fake_get(url, headers=None, timeout=None):
        return FakeResp()

    monkeypatch.setattr(weather.requests, "get", fake_get)


def _series_temps(time_defines, temps, point_name="京都"):
    return {
        "timeDefines": time_defines,
        "areas": [{"area": {"name": point_name, "code": "260010"}, "temps": temps}],
    }


def _series_temps_minmax(time_defines, temps_min, temps_max, point_name="京都"):
    return {
        "timeDefines": time_defines,
        "areas": [
            {
                "area": {"name": point_name, "code": "260000"},
                "tempsMin": temps_min,
                "tempsMax": temps_max,
            }
        ],
    }


def _series_other(point_name="京都"):
    """temps/tempsMin を持たないダミー系列(天気コード等の代わり)。"""
    return {
        "timeDefines": ["2026-09-01T00:00:00+09:00"],
        "areas": [{"area": {"name": point_name, "code": "260010"}, "weathers": ["晴れ"]}],
    }


WEATHER_CFG = {"jma_area_code": "260000", "jma_point_name": "京都"}


def test_fetch_jma_tempsは直近予報のtemps系列から日毎の最低最高を作る(monkeypatch):
    data0 = {
        "timeSeries": [
            _series_temps(
                ["2026-09-01T00:00:00+09:00", "2026-09-01T09:00:00+09:00"],
                ["27", "35"],
            )
        ]
    }
    _jma_response(monkeypatch, [data0])
    result = fetch_jma_temps(WEATHER_CFG)
    assert result == {"2026-09-01": (27.0, 35.0)}


def test_fetch_jma_tempsは週間予報のtempsMinMaxから翌日以降を埋める(monkeypatch):
    data0 = {
        "timeSeries": [
            _series_temps(
                ["2026-09-01T00:00:00+09:00", "2026-09-01T09:00:00+09:00"],
                ["27", "35"],
            )
        ]
    }
    data1 = {
        "timeSeries": [
            _series_other(),
            _series_temps_minmax(
                [
                    "2026-09-01T00:00:00+09:00",
                    "2026-09-02T00:00:00+09:00",
                    "2026-09-03T00:00:00+09:00",
                ],
                ["24", "25", "23"],
                ["34", "35", "33"],
            ),
        ]
    }
    _jma_response(monkeypatch, [data0, data1])
    result = fetch_jma_temps(WEATHER_CFG)
    # 09-01は[0]と[1]の両方にあるが、[0]の値(27.0, 35.0)が優先される。
    assert result == {
        "2026-09-01": (27.0, 35.0),
        "2026-09-02": (25.0, 35.0),
        "2026-09-03": (23.0, 33.0),
    }


def test_fetch_jma_tempsはtemps系列の添字が変わっても見つけられる(monkeypatch):
    data0 = {
        "timeSeries": [
            _series_other(),
            _series_other(),
            _series_temps(
                ["2026-09-01T00:00:00+09:00", "2026-09-01T09:00:00+09:00"],
                ["27", "35"],
            ),
        ]
    }
    _jma_response(monkeypatch, [data0])
    result = fetch_jma_temps(WEATHER_CFG)
    assert result == {"2026-09-01": (27.0, 35.0)}


def test_fetch_jma_tempsは空文字や数値でないtemps要素をスキップする(monkeypatch):
    data0 = {
        "timeSeries": [
            _series_temps(
                [
                    "2026-09-01T00:00:00+09:00",
                    "2026-09-01T06:00:00+09:00",
                    "2026-09-01T12:00:00+09:00",
                    "2026-09-01T18:00:00+09:00",
                ],
                ["", "27", "abc", "35"],
            )
        ]
    }
    _jma_response(monkeypatch, [data0])
    result = fetch_jma_temps(WEATHER_CFG)
    assert result == {"2026-09-01": (27.0, 35.0)}


def test_fetch_jma_tempsは地点名が一致しなければ空辞書を返す(monkeypatch):
    data0 = {
        "timeSeries": [
            _series_temps(
                ["2026-09-01T00:00:00+09:00", "2026-09-01T09:00:00+09:00"],
                ["27", "35"],
                point_name="東京",
            )
        ]
    }
    _jma_response(monkeypatch, [data0])
    result = fetch_jma_temps(WEATHER_CFG)
    assert result == {}


def test_fetch_jma_tempsはHTTPエラー時に空辞書を返す(monkeypatch):
    def fake_get(url, headers=None, timeout=None):
        raise ConnectionError("boom")

    monkeypatch.setattr(weather.requests, "get", fake_get)
    result = fetch_jma_temps(WEATHER_CFG)
    assert result == {}


# =========================================================
# fetch_open_meteo
# =========================================================

def test_fetch_open_meteoは風速単位msとforecast_daysをdays_plus_1で渡す(monkeypatch):
    captured = {}

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"hourly": {}}

    def fake_get(url, params=None, headers=None, timeout=None):
        captured["params"] = params
        return FakeResp()

    monkeypatch.setattr(weather.requests, "get", fake_get)
    result = fetch_open_meteo({"latitude": 35.0, "longitude": 135.0, "days": 2})

    assert result == {"hourly": {}}
    assert captured["params"]["wind_speed_unit"] == "ms"
    assert captured["params"]["forecast_days"] == 3


def test_fetch_open_meteoはエラー時にNoneを返す(monkeypatch):
    def fake_get(url, params=None, headers=None, timeout=None):
        raise ConnectionError("boom")

    monkeypatch.setattr(weather.requests, "get", fake_get)
    result = fetch_open_meteo({"latitude": 35.0, "longitude": 135.0, "days": 2})
    assert result is None


# =========================================================
# build_forecasts
# =========================================================

def _hourly_time_and_temp(entries):
    """[(iso_datetime_str, temperature), ...] から hourly 用の time/temperature 配列を作る。"""
    times = [t for t, _ in entries]
    temps = [v for _, v in entries]
    return times, temps


def test_build_forecastsは途中から始まる時刻配列でも時刻の対応付けが正しい(monkeypatch):
    """hourly.time の先頭が00時でないデータでも、配列添字ではなく時刻文字列で
    正しく対応付けられることの回帰テスト。"""

    entries = [
        # 前日の時刻を先頭に置き、day0のデータが配列の先頭(添字0)に来ないようにする
        ("2026-08-31T20:00", -1.0),
        ("2026-08-31T21:00", -2.0),
        ("2026-08-31T22:00", -3.0),
        ("2026-08-31T23:00", -4.0),
    ]
    # day0 (2026-09-01) の 1〜23時。13時はわざと欠落させる。
    for h in range(1, 24):
        if h == 13:
            continue
        entries.append((f"2026-09-01T{h:02d}:00", float(h)))
    # tenki.jp流の24時 = 翌日00時。ここに際立った値を置き、誤って別のマッピングと
    # 混同していないことを確認する。
    entries.append(("2026-09-02T00:00", 100.0))
    # day1 (2026-09-02) の 1〜24時。
    for h in range(1, 24):
        entries.append((f"2026-09-02T{h:02d}:00", float(h) + 1000.0))
    entries.append(("2026-09-03T00:00", 2000.0))

    times, temps = _hourly_time_and_temp(entries)
    n = len(times)
    om = {
        "hourly": {
            "time": times,
            "temperature_2m": temps,
            "precipitation_probability": [10] * n,
            "precipitation": [0.0] * n,
            "relative_humidity_2m": [50] * n,
            "weather_code": [0] * n,
            "wind_speed_10m": [1.0] * n,
            "wind_direction_10m": [0] * n,
        },
        "daily": {
            # Open-Meteoの実データはtimezone=Asia/Tokyo指定時、秒・オフセット無しの
            # ローカル時刻文字列("YYYY-MM-DDTHH:MM")を返す。
            "time": ["2026-09-01", "2026-09-02"],
            "sunrise": ["2026-09-01T05:29", "2026-09-02T05:30"],
            "sunset": ["2026-09-01T18:24", "2026-09-02T18:23"],
        },
    }

    monkeypatch.setattr(weather, "fetch_open_meteo", lambda cfg: om)
    monkeypatch.setattr(weather, "fetch_jma_temps", lambda cfg: {"2026-09-01": (26.0, 36.0)})

    now = datetime(2026, 9, 1, 7, 0, tzinfo=JST)
    result = build_forecasts({"days": 2}, now=now)

    assert len(result) == 2
    day0, day1 = result

    assert day0.date == date(2026, 9, 1)
    assert day0.label == "今日 09/01(火)"
    assert day1.date == date(2026, 9, 2)
    assert day1.label == "明日 09/02(水)"

    hours0 = {h.hour: h for h in day0.hours}
    # 13時は欠落したまま、例外にもならず他の時刻は残っている。
    assert 13 not in hours0
    assert hours0[1].temperature == 1.0
    assert hours0[23].temperature == 23.0
    # 24時 = 翌日00時のデータが取られている(day1の1時の値000+1と混同していない)。
    assert hours0[24].temperature == 100.0

    hours1 = {h.hour: h for h in day1.hours}
    assert hours1[1].temperature == 1001.0
    assert hours1[24].temperature == 2000.0

    assert day0.sunrise == "05:29"
    assert day0.sunset == "18:24"
    assert day0.official_low == 26.0
    assert day0.official_high == 36.0

    # day1は気象庁データが無いので official_* は None のまま。
    assert day1.official_low is None
    assert day1.official_high is None


def test_build_forecastsはfetch_open_meteoがNoneなら空リストを返す(monkeypatch):
    monkeypatch.setattr(weather, "fetch_open_meteo", lambda cfg: None)
    monkeypatch.setattr(weather, "fetch_jma_temps", lambda cfg: {})
    result = build_forecasts({"days": 2})
    assert result == []


def test_build_forecastsはhoursが0件の日を結果に含めない(monkeypatch):
    om = {"hourly": {"time": []}, "daily": {}}
    monkeypatch.setattr(weather, "fetch_open_meteo", lambda cfg: om)
    monkeypatch.setattr(weather, "fetch_jma_temps", lambda cfg: {})
    result = build_forecasts({"days": 1})
    assert result == []


def test_build_forecastsは内部で例外が起きても空リストを返す(monkeypatch):
    def broken_fetch(cfg):
        raise RuntimeError("boom")

    monkeypatch.setattr(weather, "fetch_open_meteo", broken_fetch)
    monkeypatch.setattr(weather, "fetch_jma_temps", lambda cfg: {})
    result = build_forecasts({"days": 2})
    assert result == []


# =========================================================
# format_day_forecast (notify_discord.py)
# =========================================================

def _hour(hour, code=0, temp=25.0, pop=10):
    return HourPoint(
        hour=hour,
        weather_code=code,
        temperature=temp,
        pop=pop,
        precipitation=0.0,
        humidity=50,
        wind_speed=1.0,
        wind_direction=0,
    )


def test_format_day_forecastは気象庁の日最高最低があればそのまま表示する():
    day = DayForecast(
        date=date(2026, 9, 1),
        label="今日 09/01(火)",
        hours=[_hour(9, temp=26.0), _hour(15, temp=35.0)],
        official_low=26.0,
        official_high=36.0,
    )
    text = format_day_forecast(day)
    assert "最高36℃ / 最低26℃" in text
    assert "(参考)" not in text.split("\n")[0]


def test_format_day_forecastは気象庁データが無ければ毎時値から参考付きで表示する():
    day = DayForecast(
        date=date(2026, 9, 1),
        label="今日 09/01(火)",
        hours=[_hour(9, temp=26.0), _hour(15, temp=35.0)],
    )
    text = format_day_forecast(day)
    header = text.split("\n")[0]
    assert "最高35℃ / 最低26℃(参考)" in header


def test_format_day_forecastはsunrise_sunsetがNoneなら余分な空白なく省略される():
    day = DayForecast(
        date=date(2026, 9, 1),
        label="今日 09/01(火)",
        hours=[_hour(9)],
        official_low=26.0,
        official_high=36.0,
        sunrise=None,
        sunset=None,
    )
    header = format_day_forecast(day).split("\n")[0]
    assert "🌅" not in header
    assert "🌇" not in header
    assert not header.endswith(" ")
    assert "  " not in header.replace("　", "")  # 半角スペースの連続が残っていない


def test_format_day_forecastはpop_thresholdを境に傘マークの有無が変わる():
    day = DayForecast(
        date=date(2026, 9, 1),
        label="今日 09/01(火)",
        hours=[_hour(1, pop=49), _hour(2, pop=49), _hour(7, pop=50), _hour(8, pop=50)],
        official_low=20.0,
        official_high=30.0,
    )
    text = format_day_forecast(day, pop_threshold=50)
    lines = {line.split("`")[1]: line for line in text.split("\n") if line.startswith("`")}
    # 未明ブロック(1-3時)はpop=49で閾値未満 → ☔なし
    assert "☔" not in lines["未明　　　"]
    # 朝ブロック(7-9時)はpop=50で閾値以上 → ☔あり
    assert "☔50%" in lines["朝　　　　"]


def test_format_day_forecastの時間帯ラベルは全角スペースで5文字幅に揃っている():
    day = DayForecast(
        date=date(2026, 9, 1),
        label="今日 09/01(火)",
        hours=[_hour(h) for h in range(1, 25)],
        official_low=20.0,
        official_high=30.0,
    )
    text = format_day_forecast(day)
    for line in text.split("\n"):
        if line.startswith("`"):
            block_label = line.split("`")[1]
            assert len(block_label) == 5


# =========================================================
# notify_weather
# =========================================================

def test_notify_weatherはdry_runならトークンが無くてもembed内容をprintする(capsys):
    """回帰テスト: 以前はトークン欠如の判定がdry_runより先にあり、ローカルで
    内容を確認できなかった。dry_runなら常にprintされることを保証する。"""
    day = DayForecast(
        date=date(2026, 9, 1),
        label="今日 09/01(火)",
        hours=[_hour(9, temp=26.0)],
        official_low=26.0,
        official_high=36.0,
    )
    weather_cfg = {"label": "京都市", "pop_alert_threshold": 50}
    env = {}  # DISCORD_BOT_TOKEN / DISCORD_CHANNEL_ID とも未設定

    sent = notify_weather([day], weather_cfg, env, "2026-09-01", dry_run=True)

    captured = capsys.readouterr()
    assert sent == 0
    assert "京都市の天気" in captured.out
    assert "最高36℃" in captured.out
