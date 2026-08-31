"""pytest共有設定。

- src/ がパッケージ化されていない(フラット構成)ため、src/ を sys.path に追加して
  各テストファイルから `from cluster import ...` のように直接importできるようにする。
  同じ問題の解き方は arxiv-bot/tests/test_judge_translate.py を参照した。
- Article / Cluster を組み立てる定型作業を減らすためのファクトリfixtureも提供する。
"""

import itertools
import os
import sys
from datetime import date, datetime, timedelta, timezone

import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from models import Article, Cluster  # noqa: E402


def pytest_configure(config):
    """tmp_pathの既定の置き場所(%TEMP%\\pytest-of-<user>)へのアクセスが拒否される
    環境(このマシンで観測済み。原因はOS側の一時ディレクトリのロックで、news-bot
    本体とは無関係)を回避し、リポジトリ内の書き込み可能な場所を使う。
    ユーザーが明示的に --basetemp を指定した場合はそちらを優先する。
    """
    if config.option.basetemp is None:
        config.option.basetemp = os.path.join(os.path.dirname(__file__), ".pytest_tmp")

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yml")

# JST(日本標準時)。fetch_feeds.pyがpublishedに詰めるタイムゾーンに揃える。
JST = timezone(timedelta(hours=9))

# make_article() が既定URLを作るときに使う連番(テスト間で重複しないように)。
_url_counter = itertools.count(1)


def jst(hour: int, minute: int = 0, day: int = 31, month: int = 8, year: int = 2026) -> datetime:
    """テスト用にJSTのdatetimeを簡潔に作るヘルパー関数。既定日は2026-08-31。"""
    return datetime(year, month, day, hour, minute, tzinfo=JST)


@pytest.fixture
def jst_time():
    """jst()関数そのものを返すfixture(テスト内で `jst_time(10)` のように使う)。"""
    return jst


@pytest.fixture
def make_article():
    """Articleを簡単に作るファクトリfixture。省略したフィールドには妥当な既定値を入れる。"""

    def _make(
        title,
        url=None,
        source="test_source",
        outlet="テスト媒体",
        lane="general",
        published=None,
        lead=None,
        representative_ok=True,
        signal=None,
    ):
        if url is None:
            url = f"https://example.com/article-{next(_url_counter)}"
        return Article(
            title=title,
            url=url,
            source=source,
            outlet=outlet,
            lane=lane,
            published=published,
            lead=lead,
            representative_ok=representative_ok,
            signal=signal,
        )

    return _make


@pytest.fixture
def make_cluster():
    """Clusterを簡単に作るファクトリfixture。

    lane / representativeを省略した場合はarticles[0]を使う(本物の名寄せロジックの
    再現が必要なテストでは cluster_articles() 自体を使うこと。このfixtureは
    「クラスタが既に出来ている」前提のrank.py / summarize.pyのテスト向け)。
    """

    def _make(articles, lane=None, representative=None, hatena_count=0):
        if lane is None:
            lane = articles[0].lane
        if representative is None:
            representative = articles[0]
        return Cluster(
            articles=articles,
            lane=lane,
            representative=representative,
            hatena_count=hatena_count,
        )

    return _make


@pytest.fixture
def today():
    """rank.pyのTTL・既報記録テストで使う基準日(実データの日付に揃える)。"""
    return date(2026, 8, 31)


@pytest.fixture
def real_config():
    """実際のconfig.ymlを読み込むfixture。重みが実運用の値と一致していることを確認する。"""
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


@pytest.fixture
def general_weights(real_config):
    """config.ymlの総合レーンのweights。"""
    return next(lane for lane in real_config["lanes"] if lane["id"] == "general")["weights"]


@pytest.fixture
def tech_weights(real_config):
    """config.ymlのテックレーンのweights。"""
    return next(lane for lane in real_config["lanes"] if lane["id"] == "tech")["weights"]
