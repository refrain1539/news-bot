"""
名寄せ(クラスタリング)モジュール。

複数の新聞社・ニュースサイトが同じ事件を報じたとき、タイトルの文字 bigram
集合の Jaccard 係数で類似度を測り、貪欲な single-link クラスタリングで
1つのトピック(Cluster)にまとめる。

- 新規依存なし。標準ライブラリ(unicodedata)のみを使う。
- 入力の順序やタイムスタンプが同じであれば、常に同じ出力になるようにする
  (決定的でないと、通知内容が実行のたびに変わってしまう)。
- クラスタの lane / representative の選び方は models.py の Cluster docstring
  が「契約」なので、そちらを変更せずここで厳密に従う。
"""

import unicodedata

from models import Article, Cluster

# 重なり係数が閾値を超えていても、共通する bigram がこの数に満たなければ
# 同一話題とみなさない。重なり係数は片方の見出しが極端に短いと少数の一致でも
# 1.0 に近づくため、絶対数の下限で暴発を止める。
# 2026-08-31 の実データでは、正解ペアの共通 bigram の最小が5、
# 誤マッチの最大が3だったので、その間を取って4にしている。
MIN_SHARED_BIGRAMS = 4

# normalize_title() で除去する記号類。
# 括弧・引用符・区切り記号のみを対象にし、漢字・かな・英数字は残す。
# 削りすぎると別の話題まで一致してしまうため、対象は最小限にとどめている。
_REMOVE_CHARS = set(
    "「」『』【】［］[]()（）〈〉《》"  # 括弧類
    "'\"‘’“”"  # 引用符(直線・カーブ双方)
    "、。，．,.・:：;；!！?？…‥—―-ー~〜|｜/"  # 区切り記号
)


def normalize_title(title: str) -> str:
    """
    名寄せ用にタイトルを正規化する。

    NFKC 正規化で全角英数字・記号を半角化し、小文字化したうえで、比較のノイズに
    なる空白類・括弧・引用符・句読点を取り除く。漢字・かな・英数字はそのまま
    残す(削りすぎると別の話題まで一致してしまうため)。
    戻り値は空文字になりうる(その場合 bigrams() は空集合を返す)。
    """
    text = unicodedata.normalize("NFKC", title)
    text = text.lower()
    return "".join(ch for ch in text if not ch.isspace() and ch not in _REMOVE_CHARS)


def bigrams(text: str) -> set[str]:
    """連続する2文字の集合を返す。1文字以下なら、その1文字だけの集合を返す(空文字なら空集合)。"""
    if len(text) <= 1:
        return {text} if text else set()
    return {text[i : i + 2] for i in range(len(text) - 1)}


def jaccard(a: set[str], b: set[str]) -> float:
    """Jaccard 係数 |a∩b| / |a∪b|。どちらかが空集合ならゼロ除算を避けて 0.0 を返す。

    名寄せの本番の指標は overlap_coefficient() であり、こちらは比較・検証用に残している。
    理由は overlap_coefficient() の docstring を参照。
    """
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def overlap_coefficient(a: set[str], b: set[str]) -> float:
    """
    重なり係数(Szymkiewicz-Simpson) |a∩b| / min(|a|,|b|)。

    名寄せに Jaccard ではなくこちらを使う理由:

    Yahoo!トピックスの見出しは13〜16字に固定されている(いわゆる13文字ルール)のに対し、
    朝日新聞やITmediaの見出しは23〜54字ある。Jaccard は分母が和集合なので、短い見出しは
    どれだけ内容が一致していても構造的に高いスコアを出せない。2026-08-31 の実データでは、
    Yahoo!トピックスと他社の「同じ話題」のペアですら Jaccard は 0.139〜0.333 にとどまり、
    閾値 0.35 を1件も超えなかった(227記事が213トピックにしか、まとまらなかった)。

    同じペアを重なり係数で測ると 0.385〜0.857 になり、無関係なペアの最大値 0.214 と
    はっきり分離する。短い見出しが長い見出しの部分集合になっている、という
    このデータの実際の構造に合った指標がこちらであるため、本番はこれを使う。

    ただし重なり係数は片方が極端に短いと暴発しやすいので、呼び出し側で
    「共通 bigram の絶対数が MIN_SHARED_BIGRAMS 以上」という下限も併せて課す。
    """
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def _published_sort_key(published):
    """published 昇順・None は最後、というソートキー。

    (None かどうか, 値) のタプルにすることで None 同士を比較させない
    (published 同士・None 同士はタプルの等値比較で解決し、異なる datetime を
    比較する場面には辿り着かない)。
    """
    return (published is None, published)


def pick_representative(articles: list[Article]) -> Article:
    """
    クラスタ代表記事を選ぶ。models.py の Cluster.representative の docstring に
    従い、以下の優先順位で決める:
      1. representative_ok かつ lead を持つ記事のうち published が最も古いもの
      2. representative_ok な記事のうち published が最も古いもの
      3. どれも representative_ok でなければ articles[0]
    published が None の記事は「最も古い」の比較で最後に回す。
    同着はリスト内の出現順で先のものを採る。
    """

    def pick_oldest(candidates: list[Article]) -> Article:
        best = candidates[0]
        for a in candidates[1:]:
            if a.published is None:
                # None は「最も古い」の対象外(常に最後に回す)。
                # 既存の best が None であっても、先に出現した方を優先するため
                # ここでは置き換えない。
                continue
            if best.published is None or a.published < best.published:
                best = a
        return best

    tier1 = [a for a in articles if a.representative_ok and a.lead]
    if tier1:
        return pick_oldest(tier1)

    tier2 = [a for a in articles if a.representative_ok]
    if tier2:
        return pick_oldest(tier2)

    return articles[0]


def _cluster_lane(member_articles: list[Article], representative: Article) -> str:
    """クラスタ内で最も多く出現するレーンを返す。同数なら代表記事のレーンを採る。

    同数時に「先に追加された記事」ではなく代表記事を優先するのは、Google ニュースの
    総合セクションとテックセクションが同じ記事を両方配信することがあり、名寄せの
    結果 general と tech が 1対1 で並ぶケースが実際に起きるため。このとき配信時刻の
    早い方(たいてい Google 側)でレーンを決めると、総合の話題がテック枠を1つ潰す。
    実際に通知に出す代表記事のレーンに合わせるほうが結果と一致する。
    """
    counts: dict[str, int] = {}
    for a in member_articles:
        counts[a.lane] = counts.get(a.lane, 0) + 1

    best_count = max(counts.values())
    tied = [lane for lane, count in counts.items() if count == best_count]
    if len(tied) == 1:
        return tied[0]
    if representative.lane in tied:
        return representative.lane
    # 代表のレーンが同数勢に含まれない場合だけ、挿入順(先に登場したレーン)に従う。
    return tied[0]


def similarity(a: set[str], b: set[str], min_shared: int = MIN_SHARED_BIGRAMS) -> float:
    """
    名寄せに使う類似度。重なり係数を返すが、共通 bigram が min_shared 未満なら 0.0 にする。

    下限を課す理由は overlap_coefficient() の docstring を参照。
    """
    shared = len(a & b)
    if shared < min_shared:
        return 0.0
    return overlap_coefficient(a, b)


def cluster_articles(
    articles: list[Article],
    threshold: float,
    min_shared_bigrams: int = MIN_SHARED_BIGRAMS,
) -> list[Cluster]:
    """
    貪欲な single-link クラスタリングで記事を名寄せする。

    1. published 昇順(None は最後)、同着は入力順で安定ソートする。
    2. 各記事の正規化タイトルの bigram 集合を1度だけ計算してキャッシュする。
    3. 既存クラスタを作成順に見て、クラスタ内のいずれかの記事との similarity() が
       threshold 以上なら、最も高い類似度のクラスタに加える(single-link)。
       同点なら先に作られたクラスタを選ぶ。どのクラスタとも満たなければ
       新しいクラスタを作る。
    4. 戻り値は各クラスタの代表記事の published 昇順(None は最後)で返す。

    同じ入力からは常に同じ出力になる(決定的)。
    """
    sorted_articles = sorted(articles, key=lambda a: _published_sort_key(a.published))

    # bigram 集合はタイトルごとに1度だけ計算し、クラスタ内の全比較で使い回す。
    bigram_cache = [bigrams(normalize_title(a.title)) for a in sorted_articles]

    # 内部表現: 各クラスタは所属記事とその bigram 集合を作成順(=追加順)に持つ。
    working_clusters: list[dict] = []

    for article, article_bigrams in zip(sorted_articles, bigram_cache):
        best_cluster = None
        best_score = -1.0

        for wc in working_clusters:
            # single-link: クラスタ内のどの記事との類似度が一番高いかを見る。
            max_sim = 0.0
            for member_bigrams in wc["bigrams"]:
                sim = similarity(article_bigrams, member_bigrams, min_shared_bigrams)
                if sim > max_sim:
                    max_sim = sim
            if max_sim >= threshold and max_sim > best_score:
                best_score = max_sim
                best_cluster = wc

        if best_cluster is not None:
            best_cluster["articles"].append(article)
            best_cluster["bigrams"].append(article_bigrams)
        else:
            working_clusters.append({"articles": [article], "bigrams": [article_bigrams]})

    result = []
    for wc in working_clusters:
        member_articles = wc["articles"]
        representative = pick_representative(member_articles)
        result.append(
            Cluster(
                articles=member_articles,
                lane=_cluster_lane(member_articles, representative),
                representative=representative,
            )
        )

    result.sort(key=lambda c: _published_sort_key(c.representative.published))

    multi_outlet = sum(1 for c in result if c.outlet_count > 1)
    print(
        f"[cluster] {len(articles)}件の記事を{len(result)}件のトピックにまとめました"
        f"(うち複数社報道: {multi_outlet}件)"
    )
    return result
