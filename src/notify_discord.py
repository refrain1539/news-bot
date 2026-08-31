"""
Discord 通知モジュール(Bot Token による REST 送信のみ)。

- Gateway(WebSocket)には接続しない。GitHub Actions から cron で叩くだけで完結する
- news-bot は **1レーン = 1メッセージ = 1 embed** で投稿する(POST /channels/{cid}/messages)。
  arxiv-bot(1論文=1メッセージ)とは異なり、10本のニュースを1通にまとめる。
  レーンごとに独立した embed を送るので、総合レーンで1通、テックレーンで1通になる。
- リアクション付与・解説書の添付リプライは行わない(news-bot にフィードバック学習機能はない)。
- Discord の embed 制限に合わせて description を切り詰める
  (title 256字 / description 4096字 / 1メッセージ内の全embed合計 6000字)。
  ただし単純な末尾切り詰めではなく、トピック単位で入るところまで入れ、
  入りきらなかったトピックは丸ごと落とす(見出しの途中で切れた壊れたトピックを出さないため)。
- 各ニュースの URL は <...> で囲み、Discord 側のリンクプレビュー展開を抑制する
  (10本分のプレビューカードが展開されると通知が壊滅的に読みにくくなるため)。
- 429 (レート制限) が返った場合は retry_after 秒だけ待って再送する

環境変数:
  DISCORD_BOT_TOKEN  ... Discord Developer Portal で発行した Bot Token
  DISCORD_CHANNEL_ID ... 投稿先チャンネルのID(開発者モードでコピーできる)
dry_run=True のときは送信せず、組み立てた embed の title と description を
そのまま(切り詰めずに)print するだけにする(main.py の流儀に合わせる)。
"""

import time

import requests

from models import Cluster, DayForecast

DISCORD_API_BASE = "https://discord.com/api/v10"

# Discord の embed 制限
EMBED_TITLE_MAX = 256
EMBED_DESCRIPTION_MAX = 4096
EMBED_TOTAL_MAX = 6000

# レーンIDごとの embed 色。config.yml に無い/未知の lane_id は DEFAULT_COLOR にフォールバックする。
LANE_COLORS = {"general": 0x3498DB, "tech": 0x1ABC9C}
DEFAULT_COLOR = 0x95A5A6

# 媒体数・はてブ数の表示閾値
HATENA_DISPLAY_MIN = 3

# 429 を受けたときの再試行回数と、1回あたりの最大待機秒数。
# Actions のジョブが待ちっぱなしにならないよう上限を設ける。
MAX_RETRIES = 3
MAX_RETRY_WAIT_SEC = 60
# 連投によるレート制限を避けるため、メッセージ間に少しだけ間隔を空ける
SEND_INTERVAL_SEC = 0.5

TIMEOUT_SEC = 30


def _headers(token):
    return {
        "Authorization": f"Bot {token}",
        "Content-Type": "application/json",
        "User-Agent": "news-bot",
    }


def _truncate(text, limit):
    """limit 字に収まるよう末尾を切り詰める(切り詰めた場合は末尾を … にする)。"""
    if not text:
        return ""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def suppress_preview(url):
    """URL を <...> で囲み、Discord のリンクプレビュー展開を抑制する。"""
    if not url:
        return ""
    return f"<{url}>"


def _retry_after_seconds(resp):
    """429 応答から待機秒数を読む。ボディが壊れていてもヘッダにフォールバックする。"""
    wait = None
    try:
        wait = resp.json().get("retry_after")
    except Exception:
        wait = None
    if wait is None:
        wait = resp.headers.get("Retry-After")
    try:
        wait = float(wait)
    except (TypeError, ValueError):
        wait = 1.0
    # 負値や極端に長い待機は握りつぶす
    return max(0.0, min(wait, MAX_RETRY_WAIT_SEC))


def discord_request(method, path, token, json_body=None, params=None):
    """
    Discord API を叩く。429 が返ったら retry_after 秒待って再試行する。
    429以外の 2xx でないレスポンスは例外を投げる。
    """
    url = f"{DISCORD_API_BASE}{path}"
    last_resp = None
    for attempt in range(MAX_RETRIES + 1):
        resp = requests.request(
            method,
            url,
            headers=_headers(token),
            json=json_body,
            params=params,
            timeout=TIMEOUT_SEC,
        )
        last_resp = resp
        if resp.status_code == 429:
            wait = _retry_after_seconds(resp)
            print(
                f"[notify_discord] レート制限(429)を受けました。{wait}秒待って再試行します "
                f"({attempt + 1}/{MAX_RETRIES})"
            )
            if attempt < MAX_RETRIES:
                time.sleep(wait)
                continue
            break
        resp.raise_for_status()
        return resp

    print(f"[notify_discord] レート制限の再試行回数({MAX_RETRIES}回)を使い切りました: {method} {path}")
    last_resp.raise_for_status()
    return last_resp


def format_topic(cluster: Cluster, rank: int) -> str:
    """
    1トピック分の Markdown 文字列を組み立てる。rank は1始まりの順位。

    書式:
      **1. 見出し**
      要約(1〜2文)  ※ summary が None/空ならこの行ごと省略する
      朝日新聞 ほか3社 ・ はてブ 87  ※ はてブは3件以上のときだけ表示
      <https://www.asahi.com/articles/...>

    トピック間の空行区切りはこの関数の責務外(呼び出し側が "\\n\\n".join する)。
    """
    lines = [f"**{rank}. {cluster.title}**"]

    if cluster.summary:
        lines.append(cluster.summary)

    # 2社のときは「A ほか1社」より両方の名前を出したほうが読みやすく、
    # 行の長さもほとんど変わらない。3社以上になると長くなるので件数表記にする。
    outlets = cluster.outlets
    if cluster.outlet_count >= 3:
        outlet_part = f"{outlets[0]} ほか{cluster.outlet_count - 1}社"
    elif cluster.outlet_count == 2:
        # メタ行の項目区切りが " ・ " なので、媒体名の連結には別の記号を使う。
        # 両方 "・" だと「GIGAZINE・Impress Watch ・ はてブ 35」のように
        # どこが区切りなのか読み取れなくなる。
        outlet_part = "、".join(outlets[:2])
    else:
        outlet_part = outlets[0] if outlets else ""

    meta_parts = [outlet_part] if outlet_part else []
    if cluster.hatena_count >= HATENA_DISPLAY_MIN:
        meta_parts.append(f"はてブ {cluster.hatena_count}")
    if meta_parts:
        lines.append(" ・ ".join(meta_parts))

    lines.append(suppress_preview(cluster.url))

    return "\n".join(lines)


def build_embed(lane_cfg: dict, clusters: list[Cluster], date_str: str) -> dict:
    """
    1レーン分の embed を組み立てる。

    description は EMBED_DESCRIPTION_MAX と
    (EMBED_TOTAL_MAX - title文字数 - footer文字数) の小さい方に収まるよう、
    トピック単位で入るところまで入れる(末尾切り詰めではなく、丸ごと落とす)。
    落としたトピックがあれば footer にその件数を書き添える。
    """
    title = _truncate(f"📰 {lane_cfg['label']}  {date_str}", EMBED_TITLE_MAX)

    topics = [format_topic(c, i + 1) for i, c in enumerate(clusters)]

    footer_text = f"{len(clusters)}件"
    budget = min(EMBED_DESCRIPTION_MAX, EMBED_TOTAL_MAX - len(title) - len(footer_text))

    included = []
    used = 0
    dropped = 0
    for topic in topics:
        # 追加後の長さ("\n\n" 区切りを含む)を見積もる
        added_len = len(topic) if not included else len(topic) + 2
        if used + added_len > budget:
            dropped += 1
            continue
        included.append(topic)
        used += added_len

    description = "\n\n".join(included)

    if dropped:
        footer_text = f"{len(clusters)}件(表示上限のため{dropped}件省略)"
        # footer_text が伸びた分、既に確定した description の予算は変えない
        # (省略件数の増減で再帰的に予算が変わるのを避けるため、そのまま採用する)

    color = LANE_COLORS.get(lane_cfg["id"], DEFAULT_COLOR)

    return {
        "title": title,
        "description": description,
        "color": color,
        "footer": {"text": footer_text},
    }


def _send_embed(embed: dict, token: str, channel_id: str, dry_run: bool = False) -> bool:
    """
    組み立て済みの embed を1メッセージとして送信する。成功したら True を返す。
    dry_run のときは送信せず、title と description を丸ごと(切り詰めずに)print する。
    """
    if dry_run:
        print(f"[notify_discord] (DRY_RUN) title: {embed['title']}")
        print(f"[notify_discord] (DRY_RUN) description:\n{embed['description']}")
        return False

    discord_request(
        "POST",
        f"/channels/{channel_id}/messages",
        token,
        json_body={"embeds": [embed]},
    )
    return True


def notify(selected: dict, lanes_cfg: list, env: dict, date_str: str, dry_run: bool = False) -> int:
    """
    レーンごとに1通ずつ Discord へ通知する。

    selected: {lane_id: [Cluster, ...]}(rank.py の戻り値)
    lanes_cfg: config.yml の lanes リスト

    lanes_cfg の順序どおりに、クラスタが1件以上あるレーンだけメッセージを送る
    (0件のレーンはスキップする)。1レーンの送信が失敗しても他のレーンは試みる。

    戻り値: 送信に成功したメッセージ数(dry_run のときは常に0)。
    """
    token = env.get("DISCORD_BOT_TOKEN")
    channel_id = env.get("DISCORD_CHANNEL_ID")

    # dry_run の判定はトークンの有無より先に行う。DRY_RUN は「本番前に中身を
    # 目で確認する」ための機能なので、トークンを持たないローカル環境でも
    # 組み立てた内容が読めなければ意味がない。
    if not dry_run and (not token or not channel_id):
        print(
            "[notify_discord] DISCORD_BOT_TOKEN / DISCORD_CHANNEL_ID が未設定のため、"
            "Discord通知をスキップします"
        )
        return 0

    sent = 0
    first = True
    for lane_cfg in lanes_cfg:
        clusters = selected.get(lane_cfg["id"]) or []
        if not clusters:
            continue

        if not first and not dry_run:
            time.sleep(SEND_INTERVAL_SEC)
        first = False

        try:
            embed = build_embed(lane_cfg, clusters, date_str)
            ok = _send_embed(embed, token, channel_id, dry_run=dry_run)
            if ok:
                sent += 1
                print(f"[notify_discord] 投稿しました: lane={lane_cfg['id']} ({len(clusters)}件)")
        except Exception as e:
            print(f"[notify_discord] 投稿に失敗しました (lane={lane_cfg['id']}): {e}")
            continue

    print(f"[notify_discord] {sent}件のメッセージを投稿しました")
    return sent


def notify_empty(env: dict, date_str: str, dry_run: bool = False) -> int:
    """
    全レーンが0件だった日に「本日は該当なし」の1通を送る。
    呼ぶかどうかの判断(notify_when_empty 設定)は main.py が行い、この関数自体は無条件に送る。

    戻り値: 送信に成功したメッセージ数(dry_run のときは0)。
    """
    token = env.get("DISCORD_BOT_TOKEN")
    channel_id = env.get("DISCORD_CHANNEL_ID")

    if not token or not channel_id:
        print(
            "[notify_discord] DISCORD_BOT_TOKEN / DISCORD_CHANNEL_ID が未設定のため、"
            "Discord通知をスキップします"
        )
        return 0

    embed = {
        "title": f"📰 本日のニュース  {date_str}",
        "description": "条件を満たすトピックがありませんでした。",
        "color": DEFAULT_COLOR,
    }

    try:
        ok = _send_embed(embed, token, channel_id, dry_run=dry_run)
    except Exception as e:
        print(f"[notify_discord] 「該当なし」通知の投稿に失敗しました: {e}")
        return 0

    if ok:
        print("[notify_discord] 「該当なし」を投稿しました")
        return 1
    return 0


# =========================================================
# 天気予報
# =========================================================
# 気温の扱いは models.py の DayForecast 周辺のコメントを参照。
# 毎時気温(Open-Meteo)は参考値、日最高/最低(official_low/high, 気象庁)が
# あればそちらを見出しに使う。無ければ毎時値の最大/最小から「(参考)」付きで出す。

WEATHER_COLOR = 0xF39C12

# 時間帯ブロック定義: (表示ラベル(全角スペースで5文字幅に揃え済み), 開始時, 終了時)
# 時刻は tenki.jp と同じ 1〜24 表記。ブロック名は tenki.jp の時間帯名に合わせている。
TIME_BLOCKS = (
    ("未明　　　", 1, 3),
    ("明け方　　", 4, 6),
    ("朝　　　　", 7, 9),
    ("昼前　　　", 10, 12),
    ("昼過ぎ　　", 13, 15),
    ("夕方　　　", 16, 18),
    ("夜のはじめ", 19, 21),
    ("夜遅く　　", 22, 24),
)


def _weather_mark(code: int) -> str:
    """
    weather.py の weather_mark() を遅延 import して使う。
    weather.py は別エージェントが同時に作成中でまだ無い/書きかけの場合があるため、
    モジュール先頭ではなく関数内で import し、ImportError は "❓" にフォールバックする。
    """
    try:
        from weather import weather_mark
    except ImportError:
        return "❓"
    return weather_mark(code)


def format_day_forecast(day: DayForecast, pop_threshold: int = 50) -> str:
    """
    1日分の天気予報を Markdown 文字列に組み立てる。

    書式(実データで描画確認済み):
      **今日 09/01(火)**　最高36℃ / 最低26℃　🌅05:29 🌇18:24
      `未明　　　` 🌤️🌤️🌤️　26〜26℃
      `明け方　　` 🌤️🌤️🌦️　25〜26℃
      `朝　　　　` 🌦️🌦️🌦️　26〜26℃　☔51%

    - 見出しの気温は has_official_temps が True なら気象庁の日最高/最低、
      False なら毎時気温の最大/最小から「(参考)」付きで出す。
      毎時データが1件も無ければ気温部分ごと省略する。
    - sunrise/sunset は None ならその部分ごと省略する(両方無ければ見出しの
      その区切り全体を省略する)。
    - 各時間帯ブロックは、対応する HourPoint が1件も無ければ行ごと省略する。
      一部の時刻だけ欠けている場合はあるだけの絵文字・気温幅で組み立てる。
    - ブロック内 pop の最大値が pop_threshold 以上のときだけ ☔ を付ける。
    """
    if day.has_official_temps:
        temp_part = f"最高{day.official_high:.0f}℃ / 最低{day.official_low:.0f}℃"
    elif day.hours:
        temp_part = (
            f"最高{max(h.temperature for h in day.hours):.0f}℃ / "
            f"最低{min(h.temperature for h in day.hours):.0f}℃(参考)"
        )
    else:
        temp_part = ""

    sun_parts = []
    if day.sunrise:
        sun_parts.append(f"🌅{day.sunrise}")
    if day.sunset:
        sun_parts.append(f"🌇{day.sunset}")

    header = f"**{day.label}**"
    if temp_part:
        header += f"　{temp_part}"
    if sun_parts:
        header += f"　{' '.join(sun_parts)}"

    lines = [header]

    hours_by_num = {h.hour: h for h in day.hours}
    for label, start, end in TIME_BLOCKS:
        block_hours = [hours_by_num[h] for h in range(start, end + 1) if h in hours_by_num]
        if not block_hours:
            continue

        marks = "".join(_weather_mark(h.weather_code) for h in block_hours)
        lo = min(h.temperature for h in block_hours)
        hi = max(h.temperature for h in block_hours)
        line = f"`{label}` {marks}　{lo:.0f}〜{hi:.0f}℃"

        max_pop = max(h.pop for h in block_hours)
        if max_pop >= pop_threshold:
            line += f"　☔{max_pop}%"

        lines.append(line)

    return "\n".join(lines)


def build_weather_embed(days: list[DayForecast], weather_cfg: dict, date_str: str) -> dict:
    """
    天気予報の embed を組み立てる。

    description は build_embed と同じ方針で、EMBED_DESCRIPTION_MAX と
    (EMBED_TOTAL_MAX - title文字数 - footer文字数) の小さい方に収まるよう、
    日単位で入るところまで入れる(入りきらない日は丸ごと落とす。
    途中で切れた時間帯表を出さないため)。

    footer には毎時気温が参考値であることを必ず明記する
    (Open-Meteo の毎時気温は気象庁の地点予報より数℃低く出るため)。
    """
    title = _truncate(
        f"⛅ {weather_cfg.get('label', '')}の天気　{date_str}", EMBED_TITLE_MAX
    )

    if any(day.has_official_temps for day in days):
        footer_text = "毎時の気温はOpen-Meteoの参考値 / 最高・最低は気象庁の地点予報"
    else:
        footer_text = "気温はOpen-Meteoの参考値"

    pop_threshold = weather_cfg.get("pop_alert_threshold", 50)
    day_texts = [format_day_forecast(day, pop_threshold) for day in days]

    budget = min(EMBED_DESCRIPTION_MAX, EMBED_TOTAL_MAX - len(title) - len(footer_text))

    included = []
    used = 0
    for text in day_texts:
        added_len = len(text) if not included else len(text) + 2
        if used + added_len > budget:
            continue
        included.append(text)
        used += added_len

    description = "\n\n".join(included)

    return {
        "title": title,
        "description": description,
        "color": WEATHER_COLOR,
        "footer": {"text": footer_text},
    }


def notify_weather(
    days: list[DayForecast], weather_cfg: dict, env: dict, date_str: str, dry_run: bool = False
) -> int:
    """
    天気予報を1通の embed として Discord へ通知する。
    天気が送れなくてもニュースの通知は続けたいため、失敗しても例外は投げない。

    戻り値: 送信に成功したメッセージ数(dry_run のときは常に0)。
    """
    if not days:
        print("[notify_discord] 天気予報データが無いため、天気通知をスキップします")
        return 0

    embed = build_weather_embed(days, weather_cfg, date_str)

    # dry_run の判定はトークンの有無より先に行う。DRY_RUN は「本番前に中身を
    # 目で確認する」ための機能なので、トークンを持たないローカル環境でも
    # 組み立てた内容が読めなければ意味がない。
    if dry_run:
        print("[notify_discord] (DRY_RUN) 天気予報の送信予定内容:")
        print(embed["title"])
        print(embed["description"])
        return 0

    token = env.get("DISCORD_BOT_TOKEN")
    channel_id = env.get("DISCORD_CHANNEL_ID")

    if not token or not channel_id:
        print(
            "[notify_discord] DISCORD_BOT_TOKEN / DISCORD_CHANNEL_ID が未設定のため、"
            "Discord通知をスキップします"
        )
        return 0

    try:
        ok = _send_embed(embed, token, channel_id, dry_run=dry_run)
    except Exception as e:
        print(f"[notify_discord] 天気予報の投稿に失敗しました: {e}")
        return 0

    if ok:
        print("[notify_discord] 天気予報を投稿しました")
        return 1
    return 0
