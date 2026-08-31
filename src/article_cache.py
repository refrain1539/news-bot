"""
記事の蓄積(article cache)モジュール。

RSS は「直近N件」のスライディングウィンドウで、1回の取得で見えるのは数時間分しか
ありません。2026-09-01 の実測では、Yahoo!トピックスは8件=1.9時間分、
Yahoo!ニュース経済は50件=4.1時間分しかカバーしておらず、朝1回だけ実行すると
その日の主要ニュースの2割弱しか候補にできません。

そこで .github/workflows/collect.yml が3時間おきに記事だけを集めてこのモジュールで
data/article_cache.jsonl に貯め、朝の本番実行では蓄積分と当日取得ぶんを合わせて
処理します(rank.py が既報を除外するのと同じく、通知するかどうかの判断はしません。
このモジュールは記事の貯蔵庫であり、選抜はしません)。

ファイル形式は JSON Lines(1行1記事)です。JSON 配列にしないのは、git の差分が
行単位になり、3時間おきのコミットで履歴が膨らみにくいからです。
"""

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from models import Article

JST = timezone(timedelta(hours=9))

# 復元に最低限必要なフィールド。どれか欠けていればレコードとして復元しない。
_REQUIRED_FIELDS = ("title", "url", "source", "outlet", "lane")


def to_record(article: Article) -> dict:
    """Article を JSONL の1行ぶんの dict に変換する。

    キーは Article のフィールド名をそのまま使う。published は
    ISO 8601 文字列にする(None ならそのまま None)。
    """
    return {
        "title": article.title,
        "url": article.url,
        "source": article.source,
        "outlet": article.outlet,
        "lane": article.lane,
        "published": article.published.isoformat() if article.published else None,
        "lead": article.lead,
        "representative_ok": article.representative_ok,
        "signal": article.signal,
        "lead_only": article.lead_only,
    }


def from_record(rec: dict) -> Optional[Article]:
    """dict から Article を復元する。壊れていれば None を返す(例外は投げない)。

    title / url / source / outlet / lane のいずれかが欠けていれば None。
    published はパースできなければレコード自体を捨てて None を返す
    (このキャッシュは published を持つ記事だけを扱うため)。復元した
    published は必ず JST に変換する。
    """
    try:
        if not isinstance(rec, dict):
            return None
        if any(not rec.get(k) for k in _REQUIRED_FIELDS):
            return None

        published_str = rec.get("published")
        if not published_str:
            return None
        try:
            published = datetime.fromisoformat(published_str)
        except (TypeError, ValueError):
            return None
        if published.tzinfo is None:
            published = published.replace(tzinfo=JST)
        published = published.astimezone(JST)

        return Article(
            title=rec["title"],
            url=rec["url"],
            source=rec["source"],
            outlet=rec["outlet"],
            lane=rec["lane"],
            published=published,
            lead=rec.get("lead"),
            representative_ok=bool(rec.get("representative_ok", True)),
            signal=rec.get("signal"),
            lead_only=bool(rec.get("lead_only", False)),
        )
    except Exception:
        return None


def load(path: str) -> list[Article]:
    """JSONL ファイルを読み込み、Article のリストを返す。

    ファイルが無ければ空リストを返す。1行ごとに JSON パースと from_record を
    試み、失敗した行はスキップしてカウントする(1行壊れていても全体を
    捨てない)。例外は投げない。
    """
    if not os.path.exists(path):
        print(f"[article_cache] キャッシュファイルが見つかりません({path})。空として扱います")
        return []

    articles: list[Article] = []
    total = 0
    broken = 0
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                total += 1
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    broken += 1
                    continue
                article = from_record(rec)
                if article is None:
                    broken += 1
                    continue
                articles.append(article)
    except Exception as e:
        print(f"[article_cache] キャッシュファイルの読み込みに失敗しました({path}): {e}。空として扱います")
        return []

    print(f"[article_cache] {total}行を読み込み、{broken}行を壊れた行として飛ばしました")
    return articles


def save(path: str, articles: list[Article], ttl_hours: int, now: datetime) -> int:
    """Article のリストを TTL・重複で間引いてから JSONL で保存する。

    - published が (now - ttl_hours) より古い記事、published が None の記事は保存しない。
    - 同一 URL は先に現れたものだけを残す。
    - published の昇順で並べてから書き出す(3時間おきの追記で差分が
      「末尾への追加 + 先頭からの削除」になるようにするため)。
    - 一時ファイルに書いてから os.replace で差し替える(書き込み途中の
      クラッシュでファイルが壊れないようにするため)。

    戻り値は保存した件数。
    """
    cutoff = now - timedelta(hours=ttl_hours)

    kept: list[Article] = []
    seen_urls: set[str] = set()
    expired = 0
    for a in articles:
        if a.published is None:
            continue
        if a.published < cutoff:
            expired += 1
            continue
        if a.url in seen_urls:
            continue
        seen_urls.add(a.url)
        kept.append(a)

    kept.sort(key=lambda a: a.published)

    dir_name = os.path.dirname(path)
    if dir_name:
        os.makedirs(dir_name, exist_ok=True)

    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        for a in kept:
            f.write(json.dumps(to_record(a), ensure_ascii=False) + "\n")
    os.replace(tmp_path, path)

    print(f"[article_cache] {len(kept)}件を保存しました(TTL {ttl_hours}時間、{expired}件を期限切れで削除)")
    return len(kept)


def merge(cached: list[Article], fresh: list[Article]) -> list[Article]:
    """蓄積分と新規取得分を URL の完全一致でマージする。

    重複は fresh 側を優先する(タイトル修正など新しい情報を持つため)。
    戻り値は published の降順(None は先頭)でソートする
    (fetch_feeds.fetch_all の戻り値と同じ並び順に揃えるため)。
    """
    by_url: dict[str, Article] = {}
    for a in cached:
        by_url[a.url] = a
    for a in fresh:
        by_url[a.url] = a

    merged = list(by_url.values())

    _FAR_FUTURE = datetime.max.replace(tzinfo=JST)
    merged.sort(
        key=lambda a: a.published if a.published is not None else _FAR_FUTURE,
        reverse=True,
    )

    print(f"[article_cache] 蓄積{len(cached)}件 + 新規取得{len(fresh)}件 → 重複を除いて{len(merged)}件")
    return merged
