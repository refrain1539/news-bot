"""
RSS/RDF フィード取得モジュール。

- config.yml の sources[] を順に取得し、Article のリストに正規化する。
  Article の定義は models.py を「契約」として、フィールドの意味はそちらに委ねる。
- フィードのパースには feedparser を使わず、標準ライブラリの
  xml.etree.ElementTree だけを使う(依存追加禁止のため)。
  そのため RSS 2.0 (<item>) と RSS 1.0/RDF ({...}item) の両方を
  自前で読み分ける必要がある。
- 実フィードは配信元によって名前空間の有無が揺れる(同じ "rdf" 形式でも
  dc:date が付いていたりいなかったりする)。子要素をローカル名(先頭の
  "{...}" を外したタグ名)で照合することで、名前空間の有無に関わらず
  同じコードで拾えるようにしている(_find_text)。
- Google ニュースのタイトルは末尾に " - 媒体名" が付き、リード文
  (description) は記事本文ではなく <ol><li><a>...</a></li></ol> という
  関連記事リンクの羅列になっている。これをそのまま lead に使うと
  意味不明な HTML の残骸が要約に渡ってしまうため、
  strip_source_suffix: true のソースでは lead を常に None にする。
- 1ソースの取得・パースに失敗しても他のソースの取得を止めない。
  日次バッチなので、1フィードが落ちていても残りを配信できることを優先する。
"""

import html
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree

import requests

from models import Article

JST = timezone(timedelta(hours=9))

# 取得時に送る User-Agent。配信元によっては UA なしのアクセスを弾くことがある。
USER_AGENT = "news-bot/1.0"
REQUEST_TIMEOUT_SEC = 20

# RDF (RSS 1.0) の item / dc:date が持つ名前空間。
_RDF_CONTENT_NS = "{http://purl.org/rss/1.0/}item"
_DC_DATE_TAG = "date"  # ローカル名で照合するため接頭辞は付けない

# HTML タグ除去用(script/style の中身ごと消したいので別扱い)
_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")

# Google ニュース形式のタイトル区切り(見出し自体に " - " を含みうるため、
# 分割は最後の出現位置で行う)
_SOURCE_SEP = " - "
_OUTLET_MAX_LEN = 40

# 見出し先頭の「[ITmedia News] 」のような媒体名の角括弧を取り除くための正規表現。
# strip_bracket_prefix: true のソースにのみ適用する。全記事に共通する文字列が
# 見出しに入っていると、名寄せの類似度が一律に底上げされてしまう。
_BRACKET_PREFIX_RE = re.compile(r"^[\[［][^\]］]{1,30}[\]］]\s*")


def strip_bracket_prefix(title):
    """見出し先頭の角括弧(媒体名)を取り除く。除去後が空になる場合は元のまま返す。"""
    stripped = _BRACKET_PREFIX_RE.sub("", title).strip()
    return stripped if stripped else title


# 見出し末尾の括弧に入った媒体名を切り出すための開き括弧・閉じ括弧。
# Yahoo!ニュースのカテゴリ別 RSS は「見出し(産経新聞)」の形式で配信元を持つ。
_OPEN_PARENS = "(（"
_CLOSE_PARENS = ")）"


def split_paren_suffix(title):
    """見出し末尾の括弧内にある媒体名を切り出し、(見出し, 媒体名) を返す。

    Yahoo!ニュースのカテゴリ別 RSS のタイトルは「見出し(産経新聞)」の形式で、
    括弧の中に実際の配信元が入っている。これを取り出さないと、Yahoo 経由の
    記事が何本あっても「Yahoo!ニュース1媒体」と数えられ、スコアの
    outlet_count 項が機能しなくなる(Google ニュースと同じ問題)。

    「見出し(テレビ朝日系(ANN))」のように括弧が入れ子になる場合があるため、
    末尾から深さを数えて対応する開き括弧を探す。全角・半角のどちらも扱う。
    括弧が無い・対応が取れない・媒体名が空か40字超なら (title, None) を返す。
    """
    text = title.rstrip()
    if not text or text[-1] not in _CLOSE_PARENS:
        return title, None

    depth = 0
    open_idx = -1
    for i in range(len(text) - 1, -1, -1):
        ch = text[i]
        if ch in _CLOSE_PARENS:
            depth += 1
        elif ch in _OPEN_PARENS:
            depth -= 1
            if depth == 0:
                open_idx = i
                break
    if open_idx <= 0:
        # 対応する開き括弧が無い、または見出し全体が括弧で囲まれている
        return title, None

    outlet = text[open_idx + 1: len(text) - 1].strip()
    headline = text[:open_idx].strip()
    if not outlet or not headline or len(outlet) > _OUTLET_MAX_LEN:
        return title, None
    return headline, outlet


def strip_html(text):
    """HTML タグを除去し、実体参照を復元したプレーンテキストを返す。

    None や空文字は "" を返す。連続する空白(改行含む)は1つの半角スペースに
    畳んで前後を trim する。
    """
    if not text:
        return ""
    without_tags = _TAG_RE.sub("", text)
    unescaped = html.unescape(without_tags)
    collapsed = _WHITESPACE_RE.sub(" ", unescaped)
    return collapsed.strip()


def split_source_suffix(title):
    """Google ニュース形式のタイトル "見出し - 媒体名" を分割する。

    区切り " - " の最後の出現位置で分割する(見出し自体に " - " が
    含まれている場合に、先頭側で誤って分割しないため)。
    区切りが無い、または媒体名が空/40字超なら (title, None) を返す。
    """
    idx = title.rfind(_SOURCE_SEP)
    if idx == -1:
        return title, None
    headline = title[:idx]
    outlet = title[idx + len(_SOURCE_SEP):].strip()
    if not outlet or len(outlet) > _OUTLET_MAX_LEN:
        return title, None
    return headline, outlet


# リード文として中身を持たない定型文。livedoor の description は記事によって
# 本文リードではなく "記事を読む" だけのことがあり、GIGAZINE は末尾に
# "続きを読む..." が付く。前者をそのまま Gemini に渡すと、見出しだけを材料に
# 内容を捏造した要約が返ってくるため、リード文なし(None)として扱う。
_LEAD_BOILERPLATE = {"記事を読む", "続きを読む", "全文を読む", "詳細を見る", "もっと見る"}
# 本文末尾に付く誘導文。中身の判定をする前に取り除く。
_LEAD_TAIL_RE = re.compile(
    r"(?:…|\.{2,})?\s*(?:続きを読む|記事を読む|全文を読む)\s*(?:…|\.{2,})?\s*$"
)
# これより短いリード文は要約の材料にならないと判断する。
_LEAD_MIN_CHARS = 15


def clean_lead(text):
    """description をリード文として使えるか判定し、使えなければ None を返す。

    末尾の「続きを読む…」のような誘導文を取り除いたうえで、定型文だけの場合と
    短すぎる場合を弾く。要約を捏造させないための入口側の防波堤。
    """
    lead = strip_html(text)
    if not lead:
        return None
    lead = _LEAD_TAIL_RE.sub("", lead).strip()
    if not lead or lead in _LEAD_BOILERPLATE:
        return None
    if len(lead) < _LEAD_MIN_CHARS:
        return None
    return lead


def _local_name(tag):
    """要素タグから "{namespace}" 部分を取り除いたローカル名を返す。"""
    if tag.startswith("{"):
        return tag.split("}", 1)[1]
    return tag


def _find_text(elem, *local_names):
    """elem の直下の子要素を、名前空間を無視したローカル名で探してテキストを返す。

    配信元によって同じ意味の要素に名前空間が付いたり付かなかったりするため、
    タグの完全一致ではなくローカル名での照合にしている。見つからなければ None。
    """
    for child in elem:
        if _local_name(child.tag) in local_names:
            return child.text
    return None


def _parse_rfc822_date(text):
    """RSS の pubDate (RFC 822 形式) を JST の datetime に変換する。失敗時は None。"""
    if not text:
        return None
    try:
        dt = parsedate_to_datetime(text.strip())
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=JST)
    return dt.astimezone(JST)


def _parse_iso8601_date(text):
    """dc:date (ISO 8601 形式) を JST の datetime に変換する。失敗時は None。"""
    if not text:
        return None
    normalized = text.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=JST)
    return dt.astimezone(JST)


def _iter_items(root, fmt):
    """フォーマットに応じて item 要素を列挙する。"""
    if fmt == "rdf":
        items = root.findall(f".//{_RDF_CONTENT_NS}")
        if not items:
            # 名前空間が付いていないフィードへのフォールバック
            items = root.findall(".//item")
        return items
    # rss
    return root.findall(".//item")


def parse_feed(xml_bytes, source_cfg):
    """フィードの XML バイト列を Article のリストに変換する。

    source_cfg は config.yml の sources[] の1要素(dict)。
    title または url が空のエントリはスキップする。
    """
    fmt = source_cfg.get("format")
    strip_suffix = bool(source_cfg.get("strip_source_suffix", False))

    root = ElementTree.fromstring(xml_bytes)
    items = _iter_items(root, fmt)

    articles = []
    for item in items:
        raw_title = _find_text(item, "title")
        raw_url = _find_text(item, "link")
        raw_description = _find_text(item, "description")

        title = strip_html(raw_title)
        url = (raw_url or "").strip()
        if not title or not url:
            continue

        outlet = None
        if strip_suffix:
            title, outlet = split_source_suffix(title)
        if source_cfg.get("strip_paren_suffix", False):
            title, paren_outlet = split_paren_suffix(title)
            if paren_outlet:
                outlet = paren_outlet
        if source_cfg.get("strip_bracket_prefix", False):
            title = strip_bracket_prefix(title)

        # フォーマット本来の日付要素を先に見るが、配信元が入れ替えている
        # ことがある(RDF なのに pubDate、RSS なのに dc:date)。片方が取れ
        # なければもう片方も試す。両方失敗しても published=None のまま
        # 記事は残す(日付が取れないだけで新着の可能性があるため)。
        if fmt == "rdf":
            published = _parse_iso8601_date(_find_text(item, _DC_DATE_TAG))
            if published is None:
                published = _parse_rfc822_date(_find_text(item, "pubDate"))
        else:
            published = _parse_rfc822_date(_find_text(item, "pubDate"))
            if published is None:
                published = _parse_iso8601_date(_find_text(item, _DC_DATE_TAG))

        if strip_suffix:
            # Google ニュースの description は記事リンクの羅列でリード文ではない
            lead = None
        else:
            lead = clean_lead(raw_description)

        articles.append(
            Article(
                title=title,
                url=url,
                source=source_cfg["id"],
                outlet=outlet if outlet else source_cfg["label"],
                lane=source_cfg["lane"],
                published=published,
                lead=lead,
                representative_ok=source_cfg.get("representative_ok", True),
                signal=source_cfg.get("signal"),
                lead_only=source_cfg.get("lead_only", False),
            )
        )

    return articles


def fetch_all(config, now=None):
    """config["sources"] を全て取得し、lookback 内の Article のリストを返す。

    1ソースの取得・パース失敗は握りつぶしてログに残し、次のソースへ進む。
    同一 URL の重複はソース内・全体を通して除去する(先に出たものを残す)。
    published が None の記事は lookback による除外の対象にしない。
    戻り値は published の降順(None は先頭)でソートする。
    """
    if now is None:
        now = datetime.now(JST)
    lookback_hours = config["lookback_hours"]
    cutoff = now - timedelta(hours=lookback_hours)

    all_articles = []
    seen_urls = set()

    for source_cfg in config["sources"]:
        source_id = source_cfg.get("id", "?")
        try:
            resp = requests.get(
                source_cfg["url"],
                headers={"User-Agent": USER_AGENT},
                timeout=REQUEST_TIMEOUT_SEC,
            )
            resp.raise_for_status()
            articles = parse_feed(resp.content, source_cfg)
        except Exception as e:
            print(f"[fetch_feeds] {source_id} の取得に失敗しました: {e}")
            continue

        fetched_count = len(articles)
        kept = []
        for a in articles:
            if a.url in seen_urls:
                continue
            if a.published is not None and a.published < cutoff:
                continue
            seen_urls.add(a.url)
            kept.append(a)

        print(f"[fetch_feeds] {source_id}: 取得{fetched_count}件 / lookback通過{len(kept)}件")
        all_articles.extend(kept)

    # published の降順(新しい順)でソートする。published が None の記事は
    # 「日付が取れないだけで新着かもしれない」ため、日付の分からない記事として
    # 末尾に埋もれさせず先頭に出す。そのため比較用のキーとしては
    # datetime.max(全ての実日付より大きい)を代わりに使い、reverse=True の
    # 降順ソートで自然に先頭へ来るようにしている(Article.published 自体は
    # 書き換えない)。
    _FAR_FUTURE = datetime.max.replace(tzinfo=JST)
    all_articles.sort(
        key=lambda a: a.published if a.published is not None else _FAR_FUTURE,
        reverse=True,
    )
    return all_articles
