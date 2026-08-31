"""rank.py(スコアリング・選抜モジュール)のテスト。

最重要の性質は「レーン分離」: 総合レーンとテックレーンのスコアは絶対に混ぜて
比較してはいけない。テックの低スコアが総合の高スコアに食われないことを必ず検証する。
"""

import json
import math
from datetime import date

import pytest

from rank import (
    load_seen,
    mark_seen,
    normalize_url,
    rank_and_select,
    save_seen,
    score_cluster,
)


def make_config(
    general_quota=6,
    tech_quota=4,
    spill_over=False,
    general_min_score=0.0,
    tech_min_score=0.0,
    general_weights=None,
    tech_weights=None,
):
    """rank_and_select向けの最小構成config。quotaやmin_scoreを小さく制御できるようにする。"""
    return {
        "lanes": [
            {
                "id": "general",
                "label": "総合",
                "quota": general_quota,
                "min_score": general_min_score,
                "weights": general_weights or {"curated_yahoo": 3.0, "outlet_count": 1.0, "hatena": 1.5},
            },
            {
                "id": "tech",
                "label": "テック・科学",
                "quota": tech_quota,
                "min_score": tech_min_score,
                "weights": tech_weights or {"outlet_count": 1.5, "hatena": 2.5},
            },
        ],
        "spill_over": spill_over,
    }


class TestScoreCluster:
    """score_clusterの各項がconfig.ymlの重みどおりに効くことを検証する。"""

    def test_signal項がconfigの重みどおりに加点される(self, make_article, make_cluster, general_weights):
        a = make_article("見出し", signal="curated_yahoo")
        cluster = make_cluster([a])
        score, breakdown = score_cluster(cluster, general_weights)
        assert score == pytest.approx(general_weights["curated_yahoo"])
        assert breakdown["curated_yahoo"] == pytest.approx(general_weights["curated_yahoo"])

    def test_signalを持たなければその項は加点されずbreakdownにも出ない(
        self, make_article, make_cluster, general_weights
    ):
        a = make_article("見出し", signal=None)
        cluster = make_cluster([a])
        score, breakdown = score_cluster(cluster, general_weights)
        assert "curated_yahoo" not in breakdown
        assert "jiji_ranking" not in breakdown

    def test_outlet_count項はconfigの重みと媒体数マイナス1で効く(self, make_article, make_cluster, general_weights):
        a1 = make_article("見出し", outlet="媒体A")
        a2 = make_article("見出し", outlet="媒体B")
        a3 = make_article("見出し", outlet="媒体C")
        cluster = make_cluster([a1, a2, a3])
        score, breakdown = score_cluster(cluster, general_weights)
        expected = general_weights["outlet_count"] * (3 - 1)
        assert score == pytest.approx(expected)
        assert breakdown["outlet_count"] == pytest.approx(expected)

    def test_outlet_countが1のときoutlet項は0でbreakdownに現れない(self, make_article, make_cluster, general_weights):
        a = make_article("見出し", outlet="媒体A")
        cluster = make_cluster([a])
        score, breakdown = score_cluster(cluster, general_weights)
        assert "outlet_count" not in breakdown
        assert score == 0.0

    def test_hatena項はlog10でconfigの重みどおりに効く(self, make_article, make_cluster, general_weights):
        a = make_article("見出し")
        cluster = make_cluster([a], hatena_count=99)
        score, breakdown = score_cluster(cluster, general_weights)
        expected = general_weights["hatena"] * math.log10(99 + 1)
        assert score == pytest.approx(expected)
        assert breakdown["hatena"] == pytest.approx(expected)

    def test_hatena_countが0のときhatena項は0でbreakdownに現れない(self, make_article, make_cluster, general_weights):
        a = make_article("見出し")
        cluster = make_cluster([a], hatena_count=0)
        score, breakdown = score_cluster(cluster, general_weights)
        assert "hatena" not in breakdown

    def test_複数の項が合算される(self, make_article, make_cluster, general_weights):
        a1 = make_article("見出し", outlet="媒体A", signal="curated_yahoo")
        a2 = make_article("見出し", outlet="媒体B")
        cluster = make_cluster([a1, a2], hatena_count=9)
        score, breakdown = score_cluster(cluster, general_weights)
        expected = (
            general_weights["curated_yahoo"]
            + general_weights["outlet_count"] * 1
            + general_weights["hatena"] * math.log10(10)
        )
        assert score == pytest.approx(expected)
        assert set(breakdown.keys()) == {"curated_yahoo", "outlet_count", "hatena"}


class TestNormalizeUrl:
    def test_トラッキングパラメータが落ちる(self):
        url = (
            "https://example.com/a?"
            "source=rss&utm_source=x&utm_medium=y&ref=1&fbclid=2&gclid=3&cx_test=z&keep=1"
        )
        assert normalize_url(url) == "https://example.com/a?keep=1"

    def test_wwwが除去されhttpがhttpsになる(self):
        assert normalize_url("http://www.example.com/a") == "https://example.com/a"

    def test_末尾スラッシュが除去される(self):
        assert normalize_url("https://example.com/a/") == "https://example.com/a"

    def test_パスがルートのみのスラッシュは残る(self):
        assert normalize_url("https://example.com/") == "https://example.com/"

    def test_クエリのキー順が揃う(self):
        a = normalize_url("https://example.com/a?b=2&a=1")
        b = normalize_url("https://example.com/a?a=1&b=2")
        assert a == b == "https://example.com/a?a=1&b=2"

    def test_パースできない入力でも例外を投げない(self):
        for bad in ["", "not a url", "http://", "ftp://[invalid", None, 12345]:
            result = normalize_url(bad)
            assert isinstance(result, str)


class TestLoadSeen:
    def test_ファイルが存在しなければ空辞書(self, tmp_path):
        result = load_seen(str(tmp_path / "no_such_file.json"))
        assert result == {}

    def test_壊れたjsonでも空辞書で例外にならない(self, tmp_path):
        path = tmp_path / "broken.json"
        path.write_text("これはJSONではありません", encoding="utf-8")
        result = load_seen(str(path))
        assert result == {}

    def test_トップレベルが配列だと空辞書扱い(self, tmp_path):
        path = tmp_path / "array.json"
        path.write_text("[1, 2, 3]", encoding="utf-8")
        result = load_seen(str(path))
        assert result == {}


class TestSaveSeen:
    def test_ttlより古いものと不正な日付を間引く(self, tmp_path):
        path = tmp_path / "seen.json"
        seen = {
            "https://example.com/old": "2026-08-01",  # cutoffの2026-08-17より前 -> 消える
            "https://example.com/boundary": "2026-08-17",  # ちょうどcutoff -> 残る
            "https://example.com/new": "2026-08-25",  # 残る
            "https://example.com/broken": "not-a-date",  # 不正 -> 消える
        }
        save_seen(str(path), seen, ttl_days=14, today=date(2026, 8, 31))

        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved == {
            "https://example.com/boundary": "2026-08-17",
            "https://example.com/new": "2026-08-25",
        }

    def test_保存先ディレクトリが無ければ作成する(self, tmp_path):
        path = tmp_path / "nested" / "dir" / "seen.json"
        save_seen(str(path), {}, ttl_days=14, today=date(2026, 8, 31))
        assert path.exists()


def test_mark_seenはcluster_urls全てを記録する(make_article, make_cluster):
    a1 = make_article("見出し", url="https://example.com/1")
    a2 = make_article("見出し", url="https://example.com/2")
    cluster = make_cluster([a1, a2], representative=a1)

    seen = {}
    mark_seen(seen, [cluster], date(2026, 8, 31))

    assert seen[normalize_url("https://example.com/1")] == "2026-08-31"
    assert seen[normalize_url("https://example.com/2")] == "2026-08-31"


class TestRankAndSelect:
    def test_レーン分離_テックの低スコアが総合の高スコアに食われない(self, make_article, make_cluster):
        """rank.pyの最重要の性質: スコアはレーン内でしか比較しない。"""
        config = make_config(general_quota=6, tech_quota=2)

        # 総合レーン: curated_yahooシグナルでスコア3.0の候補を8件用意する(quotaは6件)。
        general_clusters = [
            make_cluster([make_article(f"総合ニュース{i}", lane="general", signal="curated_yahoo")], lane="general")
            for i in range(8)
        ]
        # テックレーン: はてブのみでスコアがずっと低い候補を3件用意する(quotaは2件)。
        tech_clusters = [
            make_cluster([make_article(f"テックニュース{i}", lane="tech")], lane="tech", hatena_count=1)
            for i in range(3)
        ]

        result = rank_and_select(general_clusters + tech_clusters, config, seen={})

        assert len(result["general"]) == 6
        assert len(result["tech"]) == 2
        # テックが選ばれた記事のスコアは、総合の最低選抜スコアより低いことを確認する
        # (スコアで食われていたら、そもそもテックが2件選ばれない)。
        assert max(c.score for c in result["tech"]) < min(c.score for c in result["general"])

    def test_min_scoreを満たす候補がquotaより少なくてもspill_over_falseなら件数が減るだけ(
        self, make_article, make_cluster
    ):
        config = make_config(general_quota=5, tech_quota=2, spill_over=False, general_min_score=1.0)

        low = make_cluster([make_article("低スコア", lane="general")], lane="general")  # スコア0 -> 足切り
        high = make_cluster(
            [make_article("高スコア", lane="general", signal="curated_yahoo")], lane="general"
        )  # スコア3.0

        result = rank_and_select([low, high], config, seen={})

        assert result["general"] == [high]  # 例外にならず、件数が減るだけ

    def test_既報除外_代表以外のurlだけがseenでもクラスタごと除外される(self, make_article, make_cluster):
        config = make_config(general_quota=5)
        rep = make_article("見出し", url="https://example.com/rep", lane="general", signal="curated_yahoo")
        other = make_article("見出し", url="https://example.com/other", lane="general")
        cluster = make_cluster([rep, other], lane="general", representative=rep)

        seen = {normalize_url("https://example.com/other"): "2026-08-20"}
        result = rank_and_select([cluster], config, seen)

        assert result["general"] == []

    def test_既報が無ければ通常どおり選抜される(self, make_article, make_cluster):
        config = make_config(general_quota=5)
        a = make_article("見出し", url="https://example.com/fresh", lane="general", signal="curated_yahoo")
        cluster = make_cluster([a], lane="general")

        result = rank_and_select([cluster], config, seen={})

        assert result["general"] == [cluster]

    def test_戻り値はconfigに登場する全レーンidを必ず含む(self, make_article, make_cluster):
        config = make_config()
        result = rank_and_select([], config, seen={})
        assert result == {"general": [], "tech": []}

    def test_並べ替えが決定的_同スコアでも順序が安定する(self, make_article, make_cluster):
        config = make_config(general_quota=10)
        clusters = [
            make_cluster(
                [make_article(f"見出し{i}", url=f"https://example.com/{i}", lane="general", signal="curated_yahoo")],
                lane="general",
            )
            for i in range(5)
        ]

        result_forward = rank_and_select(list(clusters), config, seen={})
        result_backward = rank_and_select(list(reversed(clusters)), config, seen={})

        urls_forward = [c.url for c in result_forward["general"]]
        urls_backward = [c.url for c in result_backward["general"]]
        assert urls_forward == urls_backward
        assert len(set(urls_forward)) == 5

    def test_lead_only記事のみのクラスタは候補から外れる(self, make_cluster):
        from models import Article

        config = make_config(general_quota=5)
        a = Article(
            title="見出し",
            url="https://example.com/lead-only",
            source="yahoo_sports",
            outlet="テスト媒体",
            lane="general",
            signal="curated_yahoo",
            lead_only=True,
        )
        cluster = make_cluster([a], lane="general")

        result = rank_and_select([cluster], config, seen={})

        assert result["general"] == []

    def test_lead_only記事と通常記事が混ざったクラスタは候補として残る(self, make_article, make_cluster):
        from models import Article

        config = make_config(general_quota=5)
        normal = make_article("見出し", url="https://example.com/normal", lane="general")
        lead_only_article = Article(
            title="見出し",
            url="https://example.com/lead-only-2",
            source="yahoo_sports",
            outlet="テスト媒体2",
            lane="general",
            lead_only=True,
        )
        cluster = make_cluster([normal, lead_only_article], lane="general", representative=normal)

        result = rank_and_select([cluster], config, seen={})

        assert result["general"] == [cluster]

    def test_同点のときリード文を持つクラスタが先に来る(self, make_article, make_cluster):
        config = make_config(general_quota=5)
        with_lead = make_cluster(
            [
                make_article(
                    "リードあり",
                    url="https://example.com/with-lead",
                    lane="general",
                    signal="curated_yahoo",
                    lead="要約の材料になる十分な長さのリード文です。",
                )
            ],
            lane="general",
        )
        without_lead = make_cluster(
            [make_article("リードなし", url="https://example.com/without-lead", lane="general", signal="curated_yahoo")],
            lane="general",
        )

        result = rank_and_select([without_lead, with_lead], config, seen={})

        assert result["general"][0] == with_lead
        assert result["general"][1] == without_lead

    def test_スコアが異なる場合はリード文の有無よりscoreが優先される(self, make_article, make_cluster):
        config = make_config(general_quota=5)
        high_score_no_lead = make_cluster(
            [make_article("高スコア", url="https://example.com/high", lane="general", signal="curated_yahoo")],
            lane="general",
        )
        low_score_with_lead = make_cluster(
            [
                make_article(
                    "低スコア",
                    url="https://example.com/low",
                    lane="general",
                    lead="要約の材料になる十分な長さのリード文です。",
                )
            ],
            lane="general",
        )

        result = rank_and_select([low_score_with_lead, high_score_no_lead], config, seen={})

        assert result["general"][0] == high_score_no_lead
        assert result["general"][1] == low_score_with_lead
