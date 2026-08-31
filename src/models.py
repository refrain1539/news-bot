"""
news-bot 全モジュールが共有するデータモデル。

このファイルはモジュール間の「契約」であり、各モジュール(fetch_feeds / cluster /
rank / summarize / notify_discord)はここで定義された型だけをやり取りする。
フィールドの追加・削除はパイプライン全体に影響するため、単独では変更しないこと。

用語:
  Article ... RSS の1エントリ = 1記事
  Cluster ... 同一の話題として名寄せされた記事の集合 = 通知の1単位(1トピック)
  outlet  ... 報道媒体。Google ニュース経由の記事はタイトル末尾の「 - 媒体名」から
              実際の媒体名を取り出してここに入れる。これをしないと、Google 経由の
              記事が何本あっても「Google ニュース1媒体」と数えられてしまい、
              スコアの outlet_count 項が機能しなくなる。
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class Article:
    """RSS の1エントリを正規化したもの。"""

    # 記事見出し。Google ニュースの「 - 媒体名」サフィックスは除去済みであること。
    title: str
    # 記事 URL。フィードに載っていたものをそのまま保持する(正規化前)。
    url: str
    # config.yml の sources[].id (例: "yahoo_topics", "asahi", "google_top")
    source: str
    # 通知に表示する媒体名(例: "朝日新聞")。Google 経由の記事はタイトル末尾から
    # 取り出した実際の媒体名が入る。取り出せなければ source_label をそのまま使う。
    outlet: str
    # config.yml の sources[].lane。"general" または "tech"
    lane: str
    # 配信時刻(tz-aware / JST)。フィードに無ければ None。
    published: Optional[datetime] = None
    # リード文(HTML タグ除去済みのプレーンテキスト)。無ければ None。
    # Yahoo!トピックスは見出しのみでリード文を持たないため、ここは None になる。
    lead: Optional[str] = None
    # このソースの記事をクラスタ代表(通知に出す1本)に選んでよいか。
    # Google ニュースの URL は news.google.com のリダイレクトで、リンク先として
    # 見苦しく、はてブ件数も元記事と一致しないため False にする。
    # Google 経由の記事は「何社が報じたか」の計数と名寄せのためだけに使う。
    representative_ok: bool = True
    # config.yml の sources[].signal。スコアの重み名に対応する(例: "curated_yahoo")。
    # スコアに寄与しないソースは None。
    signal: Optional[str] = None


@dataclass
class Cluster:
    """同一話題として名寄せされた記事の集合。通知の1単位。"""

    # このクラスタに属する記事(1件以上)
    articles: list[Article]
    # "general" または "tech"。クラスタ内で最も多いレーンを採用する。
    lane: str
    # 通知に出す代表記事。cluster.py が以下の優先順位で選ぶ:
    #   1. representative_ok かつ lead を持つ記事のうち published が最も古いもの
    #   2. representative_ok な記事のうち published が最も古いもの
    #   3. (全て representative_ok=False の場合) articles[0]
    representative: Article
    # はてなブックマーク件数。クラスタ内の記事 URL のうち最大値を採用する。
    hatena_count: int = 0
    # rank.py が計算した総合スコア
    score: float = 0.0
    # スコアの内訳 {重み名: 寄与点}。ログとテストで内訳を検証するために持つ。
    breakdown: dict[str, float] = field(default_factory=dict)
    # summarize.py が生成した日本語要約(2行程度)。生成できなければ None。
    # None の場合、通知は見出しとリンクのみになる(要約を捏造させない)。
    summary: Optional[str] = None

    @property
    def title(self) -> str:
        return self.representative.title

    @property
    def url(self) -> str:
        return self.representative.url

    @property
    def lead(self) -> Optional[str]:
        """クラスタ内で最初に見つかったリード文。要約の入力になる。

        代表記事がリード文を持たない場合(例: Yahoo!トピックス単独)でも、
        同じ話題の他社記事がリード文を持っていればそれを借りる。
        """
        if self.representative.lead:
            return self.representative.lead
        for a in self.articles:
            if a.lead:
                return a.lead
        return None

    @property
    def outlets(self) -> list[str]:
        """このクラスタを報じた媒体名の一覧(重複なし・出現順)。"""
        seen = []
        for a in self.articles:
            if a.outlet not in seen:
                seen.append(a.outlet)
        return seen

    @property
    def outlet_count(self) -> int:
        return len(self.outlets)

    @property
    def urls(self) -> list[str]:
        """クラスタ内の全 URL。既報判定(seen_urls)はこの全てを対象にする。

        代表記事は実行日によって入れ替わりうるため、代表の URL だけを
        記録すると同じ話題を翌日また通知してしまう。
        """
        return [a.url for a in self.articles]

    def has_signal(self, signal: str) -> bool:
        """指定のシグナル(例: "curated_yahoo")を持つ記事を含むか。"""
        return any(a.signal == signal for a in self.articles)
