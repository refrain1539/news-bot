"""fetch_feeds.py(RSS/RDF取得モジュール)のテスト。

ネットワークには一切アクセスしない。parse_feed は XML バイト列を直接渡して検証し、
fetch_all 自体(requests.get を呼ぶ部分)はテスト対象に含めない。
"""

from datetime import timedelta, timezone

import pytest

from fetch_feeds import clean_lead, parse_feed, split_source_suffix, strip_html

JST = timezone(timedelta(hours=9))


# =========================================================
# strip_html
# =========================================================

def test_strip_htmlはタグを除去し実体参照を復元し連続空白を畳む():
    text = "<p>AとB&amp;C</p>\n\n  の話 &nbsp; です<br/>"
    assert strip_html(text) == "AとB&C の話 です"


def test_strip_htmlは空文字とNoneでで空文字を返す():
    assert strip_html(None) == ""
    assert strip_html("") == ""


# =========================================================
# split_source_suffix
# =========================================================

def test_split_source_suffixは見出しと媒体名を分割する():
    headline, outlet = split_source_suffix("台風が接近 - 朝日新聞")
    assert headline == "台風が接近"
    assert outlet == "朝日新聞"


def test_split_source_suffixは見出し自体にハイフンを含む場合最後の出現位置で分割する():
    headline, outlet = split_source_suffix("A - B - 朝日新聞")
    assert headline == "A - B"
    assert outlet == "朝日新聞"


def test_split_source_suffixは区切りが無ければ媒体名Noneを返す():
    headline, outlet = split_source_suffix("見出しのみ")
    assert headline == "見出しのみ"
    assert outlet is None


def test_split_source_suffixは媒体名が40字超なら分割しない():
    long_outlet = "な" * 41
    title = f"見出し - {long_outlet}"
    headline, outlet = split_source_suffix(title)
    assert headline == title
    assert outlet is None


# =========================================================
# clean_lead
# =========================================================

def test_clean_leadはlivedoorの定型文だけのdescriptionをNoneにする():
    assert clean_lead("記事を読む") is None


def test_clean_leadは末尾の誘導文だけを除去して本文を残す():
    text = "...関係している可能性があります。続きを読む..."
    assert clean_lead(text) == "...関係している可能性があります。"


def test_clean_leadは15文字未満をNoneにする():
    assert clean_lead("短い本文です") is None


@pytest.mark.parametrize("text", [None, "", "<p></p>", "<div><span></span></div>"])
def test_clean_leadはNoneや空文字やタグだけの文字列をNoneにする(text):
    assert clean_lead(text) is None


# =========================================================
# parse_feed
# =========================================================

RSS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/">
<channel>
<title>Test Feed</title>
<item>
<title>見出しA - 朝日新聞</title>
<link>https://example.com/a</link>
<description>本文の説明として十分な長さのリード文です。</description>
<pubDate>Tue, 01 Sep 2026 07:00:00 +0900</pubDate>
</item>
<item>
<title>見出しB(pubDateが無くdc:dateにフォールバック)</title>
<link>https://example.com/b</link>
<description>フォールバック確認用の本文説明で十分な長さがあります。</description>
<dc:date>2026-09-01T08:00:00+09:00</dc:date>
</item>
<item>
<title>見出しC(dc:dateがZ付きUTC)</title>
<link>https://example.com/c</link>
<description>UTC表記のdc:dateがJSTに変換されることを確認する本文です。</description>
<dc:date>2026-09-01T00:00:00Z</dc:date>
</item>
<item>
<title>見出しD(日付パース不能)</title>
<link>https://example.com/d</link>
<description>日付が壊れていても例外にならず記事は残ることを確認する本文です。</description>
<pubDate>not-a-valid-date</pubDate>
</item>
<item>
<title></title>
<link>https://example.com/empty-title</link>
<description>タイトルが空なのでスキップされるべき記事です。</description>
</item>
<item>
<title>リンクが空の記事</title>
<link></link>
<description>リンクが空なのでスキップされるべき記事です。</description>
</item>
</channel>
</rss>
"""

RDF_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns="http://purl.org/rss/1.0/"
         xmlns:dc="http://purl.org/dc/elements/1.1/">
<item rdf:about="https://example.com/e">
<title>見出しE</title>
<link>https://example.com/e</link>
<description>RDF記事の本文説明として十分な長さがあります。</description>
<dc:date>2026-09-01T10:00:00+09:00</dc:date>
</item>
<item rdf:about="https://example.com/f">
<title>見出しF(dc:dateが無くpubDateにフォールバック)</title>
<link>https://example.com/f</link>
<description>RDF形式でもpubDateへのフォールバックを確認する本文です。</description>
<pubDate>Tue, 01 Sep 2026 11:00:00 +0900</pubDate>
</item>
</rdf:RDF>
"""

# 名前空間の宣言(xmlns="http://purl.org/rss/1.0/")が無いRDF。
# _iter_itemsのフォールバック(plainな"item"検索)を確認するためのもの。
RDF_XML_NO_NAMESPACE = """<?xml version="1.0" encoding="UTF-8"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:dc="http://purl.org/dc/elements/1.1/">
<item>
<title>見出しG</title>
<link>https://example.com/g</link>
<description>名前空間の無いRDFでも拾えることを確認する本文説明です。</description>
<dc:date>2026-09-01T12:00:00+09:00</dc:date>
</item>
</rdf:RDF>
"""

# Google ニュース形式(strip_source_suffix: true)を模したRSS。
GOOGLE_STYLE_RSS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
<item>
<title>台風が接近 - A - 毎日新聞</title>
<link>https://news.google.com/articles/xyz</link>
<description><ol><li><a href="https://example.com">関連記事</a></li></ol></description>
<pubDate>Tue, 01 Sep 2026 09:00:00 +0900</pubDate>
</item>
</channel>
</rss>
"""


def _source_cfg(**overrides):
    cfg = {
        "id": "test_source",
        "label": "テスト媒体",
        "lane": "general",
        "format": "rss",
    }
    cfg.update(overrides)
    return cfg


def test_parse_feedはRSSのpubDateとdc_dateフォールバックをJSTのdatetimeにする():
    articles = parse_feed(RSS_XML.encode("utf-8"), _source_cfg())
    by_title_prefix = {a.title[:4]: a for a in articles}

    a_pubdate = by_title_prefix["見出しA"]
    assert a_pubdate.published == datetime_jst(2026, 9, 1, 7, 0)

    b_fallback = by_title_prefix["見出しB"]
    assert b_fallback.published == datetime_jst(2026, 9, 1, 8, 0)

    c_utc = by_title_prefix["見出しC"]
    assert c_utc.published == datetime_jst(2026, 9, 1, 9, 0)


def test_parse_feedは日付がパースできなければpublishedがNoneで例外にならない():
    articles = parse_feed(RSS_XML.encode("utf-8"), _source_cfg())
    d_broken = next(a for a in articles if a.title.startswith("見出しD"))
    assert d_broken.published is None


def test_parse_feedはtitleやlinkが空のエントリをスキップする():
    articles = parse_feed(RSS_XML.encode("utf-8"), _source_cfg())
    titles = [a.title for a in articles]
    assert not any(t == "" for t in titles)
    assert not any("スキップされるべき" in (a.lead or "") for a in articles if a.title == "")
    urls = [a.url for a in articles]
    assert "" not in urls
    # 6件中、空タイトル1件・空リンク1件がスキップされ4件残る
    assert len(articles) == 4


def test_parse_feedはRDFのdc_dateとpubDateフォールバックをJSTのdatetimeにする():
    articles = parse_feed(RDF_XML.encode("utf-8"), _source_cfg(format="rdf"))
    e_dcdate = next(a for a in articles if a.title == "見出しE")
    assert e_dcdate.published == datetime_jst(2026, 9, 1, 10, 0)

    f_fallback = next(a for a in articles if a.title.startswith("見出しF"))
    assert f_fallback.published == datetime_jst(2026, 9, 1, 11, 0)


def test_parse_feedは名前空間の無いRDFでも要素を拾える():
    articles = parse_feed(RDF_XML_NO_NAMESPACE.encode("utf-8"), _source_cfg(format="rdf"))
    assert len(articles) == 1
    assert articles[0].title == "見出しG"
    assert articles[0].published == datetime_jst(2026, 9, 1, 12, 0)


def test_parse_feedはstrip_source_suffixが真ならleadが常にNoneでoutletが取れる():
    cfg = _source_cfg(id="google_top", label="Googleニュース", strip_source_suffix=True)
    articles = parse_feed(GOOGLE_STYLE_RSS_XML.encode("utf-8"), cfg)
    assert len(articles) == 1
    article = articles[0]
    # ハイフンを含む見出し自体があっても、最後の出現位置で分割される。
    assert article.title == "台風が接近 - A"
    assert article.outlet == "毎日新聞"
    assert article.lead is None


def test_parse_feedはstrip_source_suffixが真でも媒体名が取れなければlabelを使う():
    cfg = _source_cfg(id="google_top", label="Googleニュース", strip_source_suffix=True)
    xml = RSS_XML  # 通常のRSSは " - 媒体名" 形式ではない(見出しAのみ例外)
    articles = parse_feed(xml.encode("utf-8"), cfg)
    b = next(a for a in articles if a.title.startswith("見出しB"))
    assert b.outlet == "Googleニュース"
    assert b.lead is None


def datetime_jst(year, month, day, hour, minute):
    from datetime import datetime

    return datetime(year, month, day, hour, minute, tzinfo=JST)
