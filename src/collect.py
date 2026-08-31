"""
記事だけを3時間おきに集めて data/article_cache.jsonl に貯めるスクリプト。

なぜ収集だけを分けて3時間おきに回すのか:

  RSS は「直近N件」のスライディングウィンドウで配信されており、1回の取得で
  見えるのは数時間分しかない(2026-09-01 の実測: Yahoo!トピックス 8件で
  1.9時間分)。朝1回の本番実行(daily.yml, 7:10 JST)だけでは、発表から
  数時間で窓の外に流れてしまった記事(見出しやリード文)を拾えない。

  そこで本スクリプトを3時間おきに走らせ、取得した記事を data/article_cache.jsonl
  に積み上げておく。朝の本番実行はこの蓄積分と当日の取得分を合わせて処理する
  ことで、その日の主要ニュースをより広くカバーできる。

  収集側は名寄せ・スコアリング・要約・通知を一切行わない。Discord にも
  Gemini にも触れないので、GitHub Secrets のうちニュース取得に不要な
  権限(GEMINI_API_KEY, DISCORD_BOT_TOKEN 等)は渡さない。
"""

import os
import sys
from datetime import datetime, timedelta, timezone

import yaml

# src/ をフラット構成で使っているため(パッケージ化していない)、
# 同ディレクトリを import パスに通してから各モジュールを読み込む。
SRC_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(SRC_DIR)
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

import article_cache  # noqa: E402
import fetch_feeds  # noqa: E402

JST = timezone(timedelta(hours=9))

CONFIG_PATH = os.path.join(BASE_DIR, "config.yml")
CACHE_PATH = os.path.join(BASE_DIR, "data", "article_cache.jsonl")


def load_config(path=CONFIG_PATH):
    """config.yml を読み込む。"""
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def main():
    dry_run = os.environ.get("DRY_RUN") == "1"
    if dry_run:
        print("[collect] DRY_RUN モードです。キャッシュの保存は行いません")

    config = load_config()
    now = datetime.now(JST)
    print(f"[collect] 実行日時: {now.isoformat()}")

    fresh = fetch_feeds.fetch_all(config, now=now)
    print(f"[collect] 今回の取得: {len(fresh)}件")

    if not fresh:
        # 全フィードが落ちている状況。ここで空のまま保存すると既存の蓄積を
        # 空で上書きしてしまい、翌朝の通知が貧弱になる。何もせず終える。
        print("[collect] 記事を1件も取得できませんでした。キャッシュは更新しません")
        return 0

    if dry_run:
        print("[collect] DRY_RUN のため保存はスキップします")
        return 0

    cached = article_cache.load(CACHE_PATH)
    print(f"[collect] 既存の蓄積: {len(cached)}件")

    merged = article_cache.merge(cached, fresh)

    saved = article_cache.save(
        CACHE_PATH, merged, config.get("cache_ttl_hours", 36), now
    )
    print(f"[collect] 保存件数: {saved}件")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"[collect] 想定外のエラーで終了します: {e}")
        sys.exit(1)
