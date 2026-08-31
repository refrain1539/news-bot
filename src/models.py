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
from datetime import date as date_type
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
    # リード文の供給専用のソースか。True の記事だけで構成されたクラスタは
    # 通知の候補から外れる(rank.py が落とす)。
    #
    # Yahoo!トピックス「主要」にはスポーツ・芸能の話題も入るが、これらの
    # カテゴリ別 RSS を普通に追加すると、トピックスに入っていないスポーツ記事
    # 単体までニュース枠を取り合ってしまう。ユーザーは配信ジャンルとして
    # スポーツ・エンタメを選んでいないため、「トピックスが拾った話題に
    # リード文を供給する」役目だけを担わせる。
    lead_only: bool = False


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


# =========================================================
# 天気予報
# =========================================================
# 気温の扱いについて(重要):
#
# 毎時の予報を無料で出しているのは Open-Meteo だが、その気温は京都では
# 気象庁の地点予報より 3〜5℃ 低く出る(2026-09-01 実測: 気象庁 35℃ /
# tenki.jp 33.8℃ / Open-Meteo 30.3℃)。京都盆地の日中高温を格子モデルが
# 捉えきれないためで、標高差が原因ではない(69m と 41m で 0.4℃ しか動かない)。
#
# 毎時カーブを線形リスケールして日最高/最低を公式値に合わせる補正も試したが、
# 格子モデルの振幅 5.5℃ を公式の 10℃ に引き伸ばすと係数が 1.8 倍になり、
# 夕方が 34〜36℃ に張り付く非現実的な結果になったため採用していない。
#
# したがって気温は補正せず、二段で持つ:
#   official_low / official_high ... 気象庁の地点予報(正確な日最高/最低)
#   HourPoint.temperature        ... Open-Meteo の毎時値(形は信頼できる参考値)
# 通知でも「毎時の気温は参考値」と明示する。要約を捏造させないのと同じ方針。


@dataclass
class HourPoint:
    """1時間ぶんの予報。"""

    # tenki.jp と同じ 1〜24 の表記(24 は翌日の00時にあたる)
    hour: int
    # WMO weather code。絵文字と和名への対応は weather.py の WMO_CODES が持つ
    weather_code: int
    # Open-Meteo の毎時気温(℃)。上記のとおり参考値
    temperature: float
    # 降水確率(%)
    pop: int
    # 降水量(mm/h)
    precipitation: float
    # 湿度(%)
    humidity: int
    # 風速(m/s)
    wind_speed: float
    # 風向(度。0=北, 90=東)
    wind_direction: int


@dataclass
class DayForecast:
    """1日ぶんの予報。通知はこれを2件(今日・明日)並べて1メッセージにする。"""

    date: date_type
    # 通知の見出しに使う表記(例: "今日 09/01(火)")
    label: str
    # 1時から24時までの HourPoint。フィードの都合で欠ける時間がありうる
    hours: list[HourPoint]
    # "05:29" / "18:24" 形式。取得できなければ None
    sunrise: Optional[str] = None
    sunset: Optional[str] = None
    # 気象庁の地点予報による日最低/最高気温(℃)。取得できなければ None
    official_low: Optional[float] = None
    official_high: Optional[float] = None

    @property
    def has_official_temps(self) -> bool:
        return self.official_low is not None and self.official_high is not None
