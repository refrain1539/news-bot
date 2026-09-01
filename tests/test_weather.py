"""weather.py(天気予報取得モジュール)と notify_discord.py の天気予報部分のテスト。

ネットワークには一切アクセスしない。requests.get は monkeypatch で差し替え、
気象庁 JSON / Open-Meteo JSON の実データ形状を模したデータを直接与える。
"""

from datetime import date, datetime, timedelta, timezone

import pytest

import weather
from models import DayForecast, HourPoint
from notify_discord import (
    DEFAULT_HOURS_PER_BLOCK,
    DEFAULT_PRECIP_MIN_DISPLAY,
    PRECIP_HEAVY_MARK,
    PRECIP_NONE_MARK,
    build_weather_embed,
    format_day_forecast,
    format_precip_series,
    format_precip_value,
    format_precipitation,
    notify_weather,
)
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
# format_precip_value (notify_discord.py) — 最重要
# =========================================================

@pytest.mark.parametrize(
    "mm",
    [
        None, "abc", -1, -0.5,
        0, 0.04, 0.05,
        0.06, 0.3, 0.94, 0.95, 1.0, 3.44, 9.94,
        9.95, 10, 50, 150,
    ],
)
def test_format_precip_valueはあらゆる入力で必ず長さ3の文字列を返す(mm):
    assert len(format_precip_value(mm)) == 3


def test_format_precip_valueはNoneでハイフン3文字を返す():
    assert format_precip_value(None) == PRECIP_NONE_MARK


@pytest.mark.parametrize("mm", ["abc", "", "1.2.3"])
def test_format_precip_valueは数値でない文字列でハイフン3文字を返す(mm):
    assert format_precip_value(mm) == PRECIP_NONE_MARK


@pytest.mark.parametrize("mm", [-1, -0.5, -100])
def test_format_precip_valueは負値でハイフン3文字を返す(mm):
    assert format_precip_value(mm) == PRECIP_NONE_MARK


def test_format_precip_valueは0でハイフン3文字を返す():
    assert format_precip_value(0) == PRECIP_NONE_MARK


def test_format_precip_valueは0_04でハイフン3文字を返す():
    assert format_precip_value(0.04) == PRECIP_NONE_MARK


def test_format_precip_valueは0_05で銀行丸めに落ちずに0_1になる():
    """回帰テスト: round() の銀行丸め(banker's rounding)だと 0.05*10=0.5 が
    round(0.5)==0 に丸められ "0.0" になってしまう。自前の四捨五入
    (floor(x*10 + 0.5)) が効いていることを確認する境界値。"""
    assert format_precip_value(0.05) == "0.1"


@pytest.mark.parametrize(
    "mm,expected",
    [
        (0.06, "0.1"),
        (0.3, "0.3"),
        (0.94, "0.9"),
        (0.95, "1.0"),
        (1.0, "1.0"),
        (3.44, "3.4"),
        (9.94, "9.9"),
    ],
)
def test_format_precip_valueは小数第1位までの3文字を返す(mm, expected):
    assert format_precip_value(mm) == expected


@pytest.mark.parametrize("mm", [9.95, 10, 50, 150])
def test_format_precip_valueは9_95以上で10プラス表記になる(mm):
    assert format_precip_value(mm) == PRECIP_HEAVY_MARK


# =========================================================
# format_precipitation (notify_discord.py)
# =========================================================

def test_format_precipitationはNoneで空文字を返す():
    assert format_precipitation(None) == ""


def test_format_precipitationは数値でない入力で空文字を返す():
    assert format_precipitation("abc") == ""


def test_format_precipitationは負値で空文字を返す():
    assert format_precipitation(-5) == ""


def test_format_precipitationは1未満で小数第1位_mm形式を返す():
    assert format_precipitation(0.3) == "0.3mm"


def test_format_precipitationは1以上で整数_mm形式を返す():
    assert format_precipitation(3) == "3mm"


def test_format_precipitationは0_05で銀行丸めに落ちずに0_1mmになる():
    assert format_precipitation(0.05) == "0.1mm"


# =========================================================
# format_precip_series (notify_discord.py)
# =========================================================

def _hour(hour, code=0, temp=25.0, pop=10, precipitation=0.0):
    return HourPoint(
        hour=hour,
        weather_code=code,
        temperature=temp,
        pop=pop,
        precipitation=precipitation,
        humidity=50,
        wind_speed=1.0,
        wind_direction=0,
    )


def test_format_precip_seriesは時間順に3文字ずつスラッシュで連結する():
    hours = [_hour(1, precipitation=0.0), _hour(2, precipitation=0.3), _hour(3, precipitation=12)]
    result = format_precip_series(hours)
    assert result == "---/0.3/10+"


def test_format_precip_seriesは各要素が3文字で全体の長さが4件数マイナス1になる():
    hours = [_hour(h, precipitation=h * 0.1) for h in range(1, 6)]
    result = format_precip_series(hours)
    assert len(result) == 4 * len(hours) - 1
    for part in result.split("/"):
        assert len(part) == 3


def test_format_precip_seriesは空リストで空文字を返す():
    assert format_precip_series([]) == ""


# =========================================================
# format_day_forecast (notify_discord.py)
# =========================================================

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


def test_format_day_forecastはsunriseのみでsunsetが省略される():
    day = DayForecast(
        date=date(2026, 9, 1),
        label="今日 09/01(火)",
        hours=[_hour(9)],
        official_low=26.0,
        official_high=36.0,
        sunrise="05:29",
        sunset=None,
    )
    header = format_day_forecast(day).split("\n")[0]
    assert "🌅05:29" in header
    assert "🌇" not in header


def test_format_day_forecastはsunrise_sunsetが両方Noneなら省略される():
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


def test_format_day_forecastは出力にcode_blockが含まれない():
    """回帰テスト: 1時間ごとの横並び表(code block)は実機で桁がずれて撤回した。
    現在の縦並び形式は ``` を一切使わない。"""
    day = DayForecast(
        date=date(2026, 9, 1),
        label="今日 09/01(火)",
        hours=[_hour(h) for h in range(1, 25)],
        official_low=20.0,
        official_high=30.0,
    )
    text = format_day_forecast(day)
    assert "```" not in text


def test_format_day_forecastはhoursが空なら見出しだけを返す():
    day = DayForecast(
        date=date(2026, 9, 1),
        label="今日 09/01(火)",
        hours=[],
    )
    text = format_day_forecast(day)
    assert text == "**今日 09/01(火)**"
    assert "最高" not in text


def test_format_day_forecastはhoursが空でも気象庁の日最高最低があれば表示する():
    day = DayForecast(
        date=date(2026, 9, 1),
        label="今日 09/01(火)",
        hours=[],
        official_low=26.0,
        official_high=36.0,
    )
    text = format_day_forecast(day)
    assert "最高36℃ / 最低26℃" in text
    assert text.count("\n") == 0  # 時間帯の行が1行も無い


def test_format_day_forecastはhoursの順序がバラバラでも昇順に並べ直される():
    day = DayForecast(
        date=date(2026, 9, 1),
        label="今日 09/01(火)",
        hours=[_hour(3, temp=3.0), _hour(1, temp=1.0), _hour(2, temp=2.0)],
        official_low=20.0,
        official_high=30.0,
    )
    lines = format_day_forecast(day).split("\n")
    block_line = next(line for line in lines if line.startswith("`01-03`"))
    # hours_per_block既定(3)なら1〜3時が1ブロックにまとまるので、
    # 入力順に関わらず最低1℃・最高3℃になることで昇順ソートを確認する。
    assert "1〜3℃" in block_line


@pytest.mark.parametrize(
    "hours_per_block,expected_labels",
    [
        (3, ["01-03", "04-06", "07-09", "10-12", "13-15", "16-18", "19-21", "22-24"]),
        (2, ["01-02", "03-04", "05-06", "07-08", "09-10", "11-12",
             "13-14", "15-16", "17-18", "19-20", "21-22", "23-24"]),
        (1, ["01-01", "02-02", "03-03", "24-24"]),
    ],
)
def test_format_day_forecastは時間帯ラベルがhours_per_blockに応じて生成される(hours_per_block, expected_labels):
    day = DayForecast(
        date=date(2026, 9, 1),
        label="今日 09/01(火)",
        hours=[_hour(h) for h in range(1, 25)],
        official_low=20.0,
        official_high=30.0,
    )
    text = format_day_forecast(day, hours_per_block=hours_per_block)
    for label in expected_labels:
        assert f"`{label}`" in text


def test_format_day_forecastは時間帯ラベルがすべて半角5文字である():
    day = DayForecast(
        date=date(2026, 9, 1),
        label="今日 09/01(火)",
        hours=[_hour(h) for h in range(1, 25)],
        official_low=20.0,
        official_high=30.0,
    )
    text = format_day_forecast(day)
    labels = [line.split("`")[1] for line in text.split("\n")[1:]]
    for label in labels:
        assert len(label) == 5
        assert label.encode("ascii", "ignore").decode("ascii") == label  # 半角のみ


def test_format_day_forecastは降水量がprecip_min未満のブロックには傘マークが出ない():
    day = DayForecast(
        date=date(2026, 9, 1),
        label="今日 09/01(火)",
        hours=[_hour(h, precipitation=0.05) for h in range(1, 4)],
        official_low=20.0,
        official_high=30.0,
    )
    text = format_day_forecast(day, hours_per_block=3, precip_min=0.1)
    assert "☔" not in text


def test_format_day_forecastは1時間でもprecip_min以上ならブロック全時間ぶんの降水量が並ぶ():
    day = DayForecast(
        date=date(2026, 9, 1),
        label="今日 09/01(火)",
        hours=[
            _hour(1, precipitation=0.0),
            _hour(2, precipitation=0.3),
            _hour(3, precipitation=0.0),
        ],
        official_low=20.0,
        official_high=30.0,
    )
    text = format_day_forecast(day, hours_per_block=3, precip_min=0.1)
    line = next(line for line in text.split("\n") if "☔" in line)
    assert "☔`---/0.3/---`mm" in line


def test_format_day_forecastはprecip_min0_0なら全ブロックに降水量が出る():
    day = DayForecast(
        date=date(2026, 9, 1),
        label="今日 09/01(火)",
        hours=[_hour(h, precipitation=0.0) for h in range(1, 25)],
        official_low=20.0,
        official_high=30.0,
    )
    text = format_day_forecast(day, hours_per_block=3, precip_min=0.0)
    block_lines = text.split("\n")[1:]
    assert len(block_lines) == 8
    for line in block_lines:
        assert "☔" in line


def test_format_day_forecastは日の出日の入りが片方だけでも例外にならない():
    day = DayForecast(
        date=date(2026, 9, 1),
        label="今日 09/01(火)",
        hours=[_hour(9)],
        official_low=20.0,
        official_high=30.0,
        sunrise=None,
        sunset="18:24",
    )
    header = format_day_forecast(day).split("\n")[0]
    assert "🌇18:24" in header
    assert "🌅" not in header


def test_format_day_forecastは一部の時刻が欠けていても例外にならず欠けた時間は詰められる():
    """2, 8, 15時が欠けている日。各ブロックは残った時間だけで組み立てられ、
    例外は起きない。"""
    hours = [_hour(h) for h in range(1, 25) if h not in (2, 8, 15)]
    day = DayForecast(
        date=date(2026, 9, 1),
        label="今日 09/01(火)",
        hours=hours,
        official_low=20.0,
        official_high=30.0,
    )
    text = format_day_forecast(day, hours_per_block=3)
    # 例外にならず、全ブロックが出力される(どのブロックも1〜2時間は残っているため)
    lines = text.split("\n")[1:]
    assert len(lines) == 8


def test_format_day_forecastはブロック内の時間が全て欠けていると行ごと省略される():
    """1,2,3時(01-03ブロック全体)が丸ごと欠けている日。該当行が出力から消える。"""
    hours = [_hour(h) for h in range(1, 25) if h not in (1, 2, 3)]
    day = DayForecast(
        date=date(2026, 9, 1),
        label="今日 09/01(火)",
        hours=hours,
        official_low=20.0,
        official_high=30.0,
    )
    text = format_day_forecast(day, hours_per_block=3)
    assert "`01-03`" not in text
    lines = text.split("\n")[1:]
    assert len(lines) == 7  # 8ブロック中1つ省略


# =========================================================
# build_weather_embed (notify_discord.py)
# =========================================================

def test_build_weather_embedはfooterに降水量の凡例を含む():
    day = DayForecast(
        date=date(2026, 9, 1),
        label="今日 09/01(火)",
        hours=[_hour(9, temp=26.0)],
        official_low=26.0,
        official_high=36.0,
    )
    embed = build_weather_embed([day], {"label": "京都市"}, "2026-09-01")
    assert "☔は1時間ごとの降水量" in embed["footer"]["text"]


def test_build_weather_embedはofficialがあるかどうかでfooterが切り替わる():
    day_official = DayForecast(
        date=date(2026, 9, 1),
        label="今日 09/01(火)",
        hours=[_hour(9, temp=26.0)],
        official_low=26.0,
        official_high=36.0,
    )
    day_no_official = DayForecast(
        date=date(2026, 9, 2),
        label="明日 09/02(水)",
        hours=[_hour(9, temp=26.0)],
    )

    embed_with_official = build_weather_embed([day_official], {"label": "京都市"}, "2026-09-01")
    assert "気象庁" in embed_with_official["footer"]["text"]

    embed_without_official = build_weather_embed(
        [day_no_official], {"label": "京都市"}, "2026-09-01"
    )
    assert "気象庁" not in embed_without_official["footer"]["text"]
    assert "Open-Meteoの参考値" in embed_without_official["footer"]["text"]


def test_build_weather_embedはweather_cfgのhours_per_blockとprecip_min_displayをformat_day_forecastに渡す(monkeypatch):
    day = DayForecast(
        date=date(2026, 9, 1),
        label="今日 09/01(火)",
        hours=[_hour(h) for h in range(1, 25)],
        official_low=20.0,
        official_high=30.0,
    )

    captured = []

    def fake_format_day_forecast(d, hours_per_block=None, precip_min=None):
        captured.append((hours_per_block, precip_min))
        return "dummy"

    # build_weather_embed はモジュール内で format_day_forecast をグローバル参照で
    # 呼ぶので、notify_discord モジュールの属性を差し替える。
    import notify_discord

    monkeypatch.setattr(notify_discord, "format_day_forecast", fake_format_day_forecast)

    notify_discord.build_weather_embed(
        [day], {"label": "京都市", "hours_per_block": 2, "precip_min_display": 0.5}, "2026-09-01"
    )

    assert captured[-1] == (2, 0.5)


def test_build_weather_embedは既定値がわたることも確認する(monkeypatch):
    day = DayForecast(
        date=date(2026, 9, 1),
        label="今日 09/01(火)",
        hours=[_hour(h) for h in range(1, 25)],
        official_low=20.0,
        official_high=30.0,
    )

    captured = []

    def fake_format_day_forecast(d, hours_per_block=None, precip_min=None):
        captured.append((hours_per_block, precip_min))
        return "dummy"

    import notify_discord

    monkeypatch.setattr(notify_discord, "format_day_forecast", fake_format_day_forecast)

    notify_discord.build_weather_embed([day], {"label": "京都市"}, "2026-09-01")

    assert captured[-1] == (DEFAULT_HOURS_PER_BLOCK, DEFAULT_PRECIP_MIN_DISPLAY)


def test_build_weather_embedはdescriptionが長すぎる場合に日単位で落とされる(monkeypatch):
    """回帰テスト: 末尾を単純に切り詰めるのではなく、日単位で丸ごと落とす
    (途中で切れた時間帯表を出さないため)。"""
    day1 = DayForecast(date=date(2026, 9, 1), label="今日 09/01(火)", hours=[_hour(9)])
    day2 = DayForecast(date=date(2026, 9, 2), label="明日 09/02(水)", hours=[_hour(9)])

    import notify_discord

    def fake_format_day_forecast(d, hours_per_block=None, precip_min=None):
        return "A" * 3000 if d.date == date(2026, 9, 1) else "B" * 3000

    monkeypatch.setattr(notify_discord, "format_day_forecast", fake_format_day_forecast)

    embed = notify_discord.build_weather_embed([day1, day2], {"label": "京都市"}, "2026-09-01")

    assert "A" in embed["description"]
    assert "B" not in embed["description"]
    assert len(embed["description"]) <= 4096


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
    weather_cfg = {"label": "京都市"}
    env = {}  # DISCORD_BOT_TOKEN / DISCORD_CHANNEL_ID とも未設定

    sent = notify_weather([day], weather_cfg, env, "2026-09-01", dry_run=True)

    captured = capsys.readouterr()
    assert sent == 0
    assert "京都市の天気" in captured.out
    assert "最高36℃" in captured.out
