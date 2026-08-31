"""cluster.py(名寄せモジュール)のテスト。

核心は「見出しの長さが極端に違っても同じ話題を拾えること」。2026-08-31の実データから
取った実際の見出しペアを使い、overlap_coefficient(重なり係数)なら分離できるが
jaccardでは分離できないことを回帰的に検証する。
"""

import pytest

from cluster import (
    bigrams,
    cluster_articles,
    jaccard,
    normalize_title,
    overlap_coefficient,
    pick_representative,
    similarity,
)

# config.ymlのcluster_thresholdと揃える。
CLUSTER_THRESHOLD = 0.35

# 2026-08-31の実データ。まとまるべきペア(重なり係数0.385〜0.857)。
POSITIVE_PAIRS = [
    (
        "総務省 情報開示遅らせる旨投稿",
        "「ほとぼり覚めさせる」 総務省幹部がチャットで開示遅延や隠蔽相談",
    ),
    (
        "ハウス食品G 壱番屋の売却検討",
        "ハウス食品グループ本社「CoCo壱番屋」を運営する壱番屋の売却を検討",
    ),
    (
        "グランドキャニオンで大規模洪水",
        "鉄砲水で１５人不明か 米グランドキャニオン",
    ),
    (
        "元アマ横綱花田秀虎 角界入り断念",
        "元アマ横綱の花田秀虎氏は角界入り断念 ギリギリまで迷うも最終チャンスの新弟子検査受検せず",
    ),
]

# まとまってはいけないペア(重なり係数0.214、共通bigram3個)。
NEGATIVE_PAIR = (
    "家族旅行一転 5人死亡の遺族後悔",
    "煙の先に小さな「山」、救助隊員がみた惨状 44人死亡の火災で何が",
)


def _bigram_sets(title_a, title_b):
    return bigrams(normalize_title(title_a)), bigrams(normalize_title(title_b))


@pytest.mark.parametrize("title_a,title_b", POSITIVE_PAIRS)
def test_同一話題の実データペアはoverlap係数で閾値を超える(title_a, title_b):
    a, b = _bigram_sets(title_a, title_b)
    assert overlap_coefficient(a, b) >= CLUSTER_THRESHOLD


@pytest.mark.parametrize("title_a,title_b", POSITIVE_PAIRS)
def test_同一話題の実データペアはjaccardでは閾値に届かない(title_a, title_b):
    """overlap_coefficientをJaccardの代わりに本番指標に採用した理由の回帰テスト。

    Yahoo!トピックス(13〜16字)と他社(23〜54字)のように見出しの長さが極端に違うと、
    Jaccardは分母が和集合になるため正解ペアでも構造的に閾値へ届かない。このテストは
    「なぜoverlap_coefficientを使っているか」を将来にわたって保存する役割を持つ。
    """
    a, b = _bigram_sets(title_a, title_b)
    assert jaccard(a, b) < 0.35


def test_無関係なペアはoverlap係数が閾値を超えない():
    a, b = _bigram_sets(*NEGATIVE_PAIR)
    assert overlap_coefficient(a, b) < CLUSTER_THRESHOLD


@pytest.mark.parametrize("title_a,title_b", POSITIVE_PAIRS)
def test_cluster_articlesが実データの同一話題ペアを1クラスタにまとめる(make_article, title_a, title_b):
    a1 = make_article(title_a)
    a2 = make_article(title_b)
    result = cluster_articles([a1, a2], threshold=CLUSTER_THRESHOLD)
    assert len(result) == 1
    assert len(result[0].articles) == 2


def test_cluster_articlesが無関係なペアを別クラスタに分ける(make_article):
    a1 = make_article(NEGATIVE_PAIR[0])
    a2 = make_article(NEGATIVE_PAIR[1])
    result = cluster_articles([a1, a2], threshold=CLUSTER_THRESHOLD)
    assert len(result) == 2


def test_similarityはmin_shared未満なら重なり係数が高くてもゼロになる():
    a = {"ab", "bc", "cd"}
    b = {"ab", "bc", "cd", "de"}
    # 重なり係数自体は 3/3 = 1.0 だが、共通bigramが3個でMIN_SHARED_BIGRAMS(既定4)未満。
    assert overlap_coefficient(a, b) == pytest.approx(1.0)
    assert similarity(a, b) == 0.0
    # min_sharedを3まで下げれば下限をクリアし、重なり係数がそのまま返る。
    assert similarity(a, b, min_shared=3) == pytest.approx(1.0)


def test_normalize_titleが全角半角括弧句読点を正規化する():
    title = "Ａｂｃ　「テスト」、質問？"
    assert normalize_title(title) == "abcテスト質問"


def test_normalize_titleは記号だけの見出しで空文字になりうる():
    assert normalize_title("「」、。") == ""
    assert bigrams(normalize_title("「」、。")) == set()


class TestPickRepresentative:
    """Cluster.representativeのdocstringに従う3段階の優先順位のテスト。"""

    def test_tier1_leadありrepresentative_okの中で最古を選ぶ(self, make_article, jst_time):
        later = make_article("A", published=jst_time(10), lead="リード", representative_ok=True)
        earliest_with_lead = make_article("B", published=jst_time(8), lead="リード", representative_ok=True)
        earliest_without_lead = make_article("C", published=jst_time(6), lead=None, representative_ok=True)
        result = pick_representative([later, earliest_with_lead, earliest_without_lead])
        assert result is earliest_with_lead

    def test_tier1が無い場合はtier2_representative_okの中で最古を選ぶ(self, make_article, jst_time):
        ok_late = make_article("A", published=jst_time(10), lead=None, representative_ok=True)
        ok_earliest = make_article("B", published=jst_time(5), lead=None, representative_ok=True)
        not_ok = make_article("C", published=jst_time(1), lead=None, representative_ok=False)
        result = pick_representative([ok_late, ok_earliest, not_ok])
        assert result is ok_earliest

    def test_全記事がrepresentative_ok_falseならarticles0番目を返す(self, make_article, jst_time):
        first = make_article("A", published=jst_time(10), representative_ok=False)
        second = make_article("B", published=jst_time(1), representative_ok=False)
        result = pick_representative([first, second])
        assert result is first

    def test_published_noneの記事は最も古いとして選ばれない(self, make_article, jst_time):
        """published=Noneは「最後」扱いなので、実時刻を持つ記事があればそちらが優先される。"""
        none_published = make_article("A", published=None, lead="リード", representative_ok=True)
        real_published = make_article("B", published=jst_time(23, 59), lead="リード", representative_ok=True)
        result = pick_representative([none_published, real_published])
        assert result is real_published

    def test_publishedが全てnoneなら出現順で先頭を採用する(self, make_article):
        first = make_article("A", published=None, lead="リード", representative_ok=True)
        second = make_article("B", published=None, lead="リード", representative_ok=True)
        result = pick_representative([first, second])
        assert result is first


def test_cluster_articlesは入力順を入れ替えても同じグルーピングになる(make_article, jst_time):
    articles = [
        make_article(POSITIVE_PAIRS[0][0], published=jst_time(1)),
        make_article(POSITIVE_PAIRS[0][1], published=jst_time(2)),
        make_article(POSITIVE_PAIRS[1][0], published=jst_time(3)),
        make_article(POSITIVE_PAIRS[1][1], published=jst_time(4)),
        make_article(NEGATIVE_PAIR[0], published=jst_time(5)),
        make_article(NEGATIVE_PAIR[1], published=jst_time(6)),
    ]

    def grouping(articles_in):
        result = cluster_articles(articles_in, threshold=CLUSTER_THRESHOLD)
        return sorted(tuple(sorted(a.title for a in c.articles)) for c in result)

    forward = grouping(articles)
    backward = grouping(list(reversed(articles)))
    shuffled = grouping([articles[3], articles[0], articles[5], articles[1], articles[4], articles[2]])

    assert forward == backward == shuffled
    assert len(forward) == 4  # 4トピックに分かれるはず


def test_cluster_laneは同数タイなら代表記事のレーンが採られる(make_article, jst_time):
    """_cluster_laneはprivate関数なので、cluster_articles経由で挙動を検証する。

    general 2件・tech 2件と同数タイになるように同一見出しの記事を用意する。
    代表記事(leadを持つ最古の記事)がtechなら、クラスタ全体のレーンもtechになるはず。
    """
    title = POSITIVE_PAIRS[0][0]
    general_1 = make_article(title, lane="general", published=jst_time(10), lead=None)
    general_2 = make_article(title, lane="general", published=jst_time(11), lead=None)
    tech_representative = make_article(title, lane="tech", published=jst_time(1), lead="リード文")
    tech_2 = make_article(title, lane="tech", published=jst_time(12), lead=None)

    result = cluster_articles(
        [general_1, general_2, tech_representative, tech_2],
        threshold=CLUSTER_THRESHOLD,
    )

    assert len(result) == 1
    cluster = result[0]
    assert cluster.representative is tech_representative
    assert cluster.lane == "tech"
