"""article_cache.py(記事の蓄積モジュール)のテスト。

このモジュールは3時間おきに追記される data/article_cache.jsonl の読み書きを担う。
最重要の性質は「1行壊れていても全体を捨てない」こと(load)と、
「published昇順で書き出す」こと(save, gitの差分を小さくするための仕様)。
"""

import json

from article_cache import from_record, load, merge, save, to_record


class TestToRecordFromRecord:
    """to_record / from_recordの往復を検証する。"""

    def test_全フィールドが往復する(self, make_article, jst_time):
        a = make_article(
            "見出し",
            url="https://example.com/a",
            source="yahoo_topics",
            outlet="時事通信",
            lane="tech",
            published=jst_time(10, 30),
            lead="リード文です。",
            representative_ok=False,
            signal="curated_yahoo",
        )
        a.lead_only = True

        rec = to_record(a)
        restored = from_record(rec)

        assert restored.title == a.title
        assert restored.url == a.url
        assert restored.source == a.source
        assert restored.outlet == a.outlet
        assert restored.lane == a.lane
        assert restored.published == a.published
        assert restored.lead == a.lead
        assert restored.representative_ok is False
        assert restored.signal == a.signal
        assert restored.lead_only is True

    def test_signalとleadがnoneでも往復する(self, make_article, jst_time):
        a = make_article("見出し", published=jst_time(9), lead=None, signal=None)
        rec = to_record(a)
        restored = from_record(rec)
        assert restored.lead is None
        assert restored.signal is None

    def test_publishedはtz_awareのJSTとして復元される(self, make_article, jst_time):
        a = make_article("見出し", published=jst_time(12))
        rec = to_record(a)
        restored = from_record(rec)
        assert restored.published.utcoffset().total_seconds() == 9 * 3600

    def test_必須キーが欠けていればnone(self, make_article, jst_time):
        a = make_article("見出し", published=jst_time(10))
        rec = to_record(a)
        for key in ("title", "url", "source", "outlet", "lane"):
            broken = dict(rec)
            del broken[key]
            assert from_record(broken) is None

    def test_publishedが欠けていればnone(self, make_article, jst_time):
        a = make_article("見出し", published=jst_time(10))
        rec = to_record(a)
        rec["published"] = None
        assert from_record(rec) is None

    def test_publishedが日付として読めない文字列ならnone(self, make_article, jst_time):
        a = make_article("見出し", published=jst_time(10))
        rec = to_record(a)
        rec["published"] = "これは日付ではありません"
        assert from_record(rec) is None


class TestLoad:
    def test_ファイルが存在しなければ空リスト(self, tmp_path):
        result = load(str(tmp_path / "no_such_file.jsonl"))
        assert result == []

    def test_正常なjsonlを読める(self, make_article, jst_time, tmp_path):
        a1 = make_article("見出し1", published=jst_time(9))
        a2 = make_article("見出し2", published=jst_time(10))
        path = tmp_path / "cache.jsonl"
        path.write_text(
            "\n".join(json.dumps(to_record(a), ensure_ascii=False) for a in (a1, a2)) + "\n",
            encoding="utf-8",
        )

        result = load(str(path))

        assert [a.title for a in result] == ["見出し1", "見出し2"]

    def test_不正な行が混じっていても正常な行だけ読める(self, make_article, jst_time, tmp_path):
        good1 = make_article("正常1", published=jst_time(9))
        good2 = make_article("正常2", published=jst_time(11))
        broken_json_line = "これはJSONではありません"
        missing_field_rec = dict(to_record(good1))
        del missing_field_rec["title"]

        lines = [
            json.dumps(to_record(good1), ensure_ascii=False),
            broken_json_line,
            json.dumps(missing_field_rec, ensure_ascii=False),
            json.dumps(to_record(good2), ensure_ascii=False),
        ]
        path = tmp_path / "cache.jsonl"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        result = load(str(path))

        assert [a.title for a in result] == ["正常1", "正常2"]


class TestSave:
    def test_ttlを超えた記事は保存されない(self, make_article, jst_time, tmp_path):
        now = jst_time(12, day=1, month=9)
        old = make_article("古い記事", published=jst_time(10, day=30, month=8))  # 26時間以上前
        fresh = make_article("新しい記事", published=jst_time(10, day=1, month=9))
        path = tmp_path / "cache.jsonl"

        count = save(str(path), [old, fresh], ttl_hours=24, now=now)

        assert count == 1
        loaded = load(str(path))
        assert [a.title for a in loaded] == ["新しい記事"]

    def test_publishedがnoneの記事は保存されない(self, make_article, jst_time, tmp_path):
        now = jst_time(12)
        no_published = make_article("日付なし", published=None)
        has_published = make_article("日付あり", published=jst_time(10))
        path = tmp_path / "cache.jsonl"

        count = save(str(path), [no_published, has_published], ttl_hours=24, now=now)

        assert count == 1
        loaded = load(str(path))
        assert [a.title for a in loaded] == ["日付あり"]

    def test_同一urlは1件だけになる(self, make_article, jst_time, tmp_path):
        now = jst_time(12)
        a1 = make_article("最初のタイトル", url="https://example.com/dup", published=jst_time(9))
        a2 = make_article("あとのタイトル", url="https://example.com/dup", published=jst_time(10))
        path = tmp_path / "cache.jsonl"

        count = save(str(path), [a1, a2], ttl_hours=24, now=now)

        assert count == 1
        loaded = load(str(path))
        assert len(loaded) == 1

    def test_published昇順で書き出される(self, make_article, jst_time, tmp_path):
        now = jst_time(15)
        a1 = make_article("3番目", published=jst_time(12))
        a2 = make_article("1番目", published=jst_time(9))
        a3 = make_article("2番目", published=jst_time(10))
        path = tmp_path / "cache.jsonl"

        save(str(path), [a1, a2, a3], ttl_hours=24, now=now)

        loaded = load(str(path))
        assert [a.title for a in loaded] == ["1番目", "2番目", "3番目"]

    def test_保存してloadすると往復する(self, make_article, jst_time, tmp_path):
        now = jst_time(15)
        a = make_article(
            "見出し",
            url="https://example.com/roundtrip",
            outlet="時事通信",
            lane="tech",
            published=jst_time(10),
            lead="リード文です。",
            representative_ok=False,
            signal="curated_yahoo",
        )
        path = tmp_path / "cache.jsonl"

        save(str(path), [a], ttl_hours=24, now=now)
        loaded = load(str(path))

        assert len(loaded) == 1
        restored = loaded[0]
        assert restored.title == a.title
        assert restored.url == a.url
        assert restored.outlet == a.outlet
        assert restored.lane == a.lane
        assert restored.published == a.published
        assert restored.lead == a.lead
        assert restored.representative_ok is False
        assert restored.signal == a.signal

    def test_戻り値は保存件数と一致する(self, make_article, jst_time, tmp_path):
        now = jst_time(15)
        articles = [make_article(f"見出し{i}", published=jst_time(9 + i)) for i in range(3)]
        path = tmp_path / "cache.jsonl"

        count = save(str(path), articles, ttl_hours=24, now=now)

        assert count == 3

    def test_親ディレクトリが無くても作られる(self, make_article, jst_time, tmp_path):
        now = jst_time(15)
        a = make_article("見出し", published=jst_time(10))
        path = tmp_path / "sub" / "cache.jsonl"

        save(str(path), [a], ttl_hours=24, now=now)

        assert path.exists()


class TestMerge:
    def test_url重複時はfresh側が優先される(self, make_article, jst_time):
        cached = [make_article("旧タイトル", url="https://example.com/dup", published=jst_time(9))]
        fresh = [make_article("新タイトル", url="https://example.com/dup", published=jst_time(9))]

        result = merge(cached, fresh)

        assert len(result) == 1
        assert result[0].title == "新タイトル"

    def test_重複しない記事は両方残る(self, make_article, jst_time):
        cached = [make_article("蓄積分", url="https://example.com/cached", published=jst_time(9))]
        fresh = [make_article("新規分", url="https://example.com/fresh", published=jst_time(10))]

        result = merge(cached, fresh)

        urls = {a.url for a in result}
        assert urls == {"https://example.com/cached", "https://example.com/fresh"}

    def test_戻り値はpublished降順でnoneは先頭(self, make_article, jst_time):
        old = make_article("古い", url="https://example.com/old", published=jst_time(9))
        new = make_article("新しい", url="https://example.com/new", published=jst_time(12))
        no_date = make_article("日付なし", url="https://example.com/no-date", published=None)

        result = merge([old], [new, no_date])

        assert [a.title for a in result] == ["日付なし", "新しい", "古い"]
