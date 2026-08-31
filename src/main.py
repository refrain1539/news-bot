"""
news-bot のエントリポイント。

毎朝1回 GitHub Actions から実行され、以下の順に処理する:

  0. weather      ... 今日と明日の時間帯別天気を取得し、ニュースより先に1通送る
  1. fetch_feeds  ... config.yml の全ソースから RSS を取得し Article に正規化する
  2. cluster      ... 同じ話題を報じた記事を名寄せして Cluster(トピック)にまとめる
  3. hatena_count ... 各トピックのはてなブックマーク件数を取得する(重要度の指標)
  4. rank         ... レーンごとにスコアリングし、既報を除いて quota 件を選抜する
  5. summarize    ... 選抜されたトピックを Gemini で1リクエストにまとめて要約する
  6. notify       ... レーンごとに1メッセージで Discord に投稿する
  7. 既報を data/seen_urls.json に記録する

設計上の要点:

- **レーン(総合 / テック・科学)は独立した枠**であり、スコアはレーン内でしか比較しない。
  混ぜると curated_* の項が定義上いつも0になるテック記事が1本も選ばれなくなる。
  詳細は rank.py と config.yml のコメントを参照。
- **各段は失敗しても後続を止めない**。はてブが取れなくても、Gemini が落ちていても、
  見出しとリンクだけの通知には価値がある。逆に、フィードが1件も取れなかった場合だけは
  通知する中身が無いので早期終了する。
- **天気とニュースは互いに独立**。天気の取得元が落ちていてもニュースは届き、
  ニュースが0件でも天気は届く。両者は別メッセージとして送る。
- DRY_RUN=1 のときは Discord への送信・Gemini への送信・状態ファイルの保存を行わず、
  組み立てた内容をログに出すだけにする。本番トリガーを有効にする前の確認用。

環境変数:
  DISCORD_BOT_TOKEN  ... Discord Bot Token(未設定なら通知をスキップ)
  DISCORD_CHANNEL_ID ... 投稿先チャンネルID(未設定なら通知をスキップ)
  GEMINI_API_KEY     ... Gemini APIキー(未設定なら要約なしで通知を続行)
  GEMINI_MODEL       ... 任意。未設定なら config.yml の gemini_model を使う
  DRY_RUN            ... "1" のとき送信・保存を行わない
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

import cluster as cluster_mod  # noqa: E402
import fetch_feeds  # noqa: E402
import hatena_count  # noqa: E402
import notify_discord  # noqa: E402
import rank  # noqa: E402
import summarize  # noqa: E402
import weather  # noqa: E402

JST = timezone(timedelta(hours=9))

CONFIG_PATH = os.path.join(BASE_DIR, "config.yml")
SEEN_PATH = os.path.join(BASE_DIR, "data", "seen_urls.json")


def load_config(path=CONFIG_PATH):
    """config.yml を読み込む。"""
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def attach_hatena_counts(clusters):
    """
    各クラスタにはてなブックマーク件数を付ける。

    件数はクラスタ内の記事 URL の**最大値**を採る。同じ話題でも、ブックマークは
    最初に注目された1社の記事に集中するため、合計ではなく最大値が「その話題が
    どれだけ注目されたか」の指標として素直になる。

    Google ニュース経由の URL(representative_ok=False)は news.google.com への
    リダイレクトで、元記事とは別 URL として扱われ件数が付かない。問い合わせても
    必ず0になるので最初から除外する(無駄なリクエストを減らす)。
    """
    target_urls = []
    for c in clusters:
        for a in c.articles:
            if a.representative_ok:
                target_urls.append(a.url)

    counts = hatena_count.get_counts(target_urls)
    if not counts:
        return

    for c in clusters:
        values = [
            counts.get(a.url, 0) for a in c.articles if a.representative_ok
        ]
        c.hatena_count = max(values) if values else 0


def run_weather(config, date_str, dry_run):
    """
    天気予報を取得して Discord に投稿する。ニュースより先に1通送る。

    天気とニュースは互いに独立させている。天気の取得元(Open-Meteo / 気象庁)が
    落ちていてもニュースは届くべきで、逆にニュースが0件でも天気は見たいため、
    ここで発生した例外はすべて握りつぶしてログに残すだけにする。
    """
    weather_cfg = config.get("weather") or {}
    if not weather_cfg.get("enabled", False):
        print("[main] weather.enabled が false のため天気はスキップします")
        return

    try:
        days = weather.build_forecasts(weather_cfg)
    except Exception as e:
        print(f"[main] 天気の取得に失敗しました(ニュースの通知は続行します): {e}")
        return

    if not days:
        print("[main] 天気予報を取得できませんでした(ニュースの通知は続行します)")
        return

    try:
        notify_discord.notify_weather(
            days, weather_cfg, os.environ, date_str, dry_run=dry_run
        )
    except Exception as e:
        print(f"[main] 天気の通知に失敗しました(ニュースの通知は続行します): {e}")


def main():
    dry_run = os.environ.get("DRY_RUN") == "1"
    if dry_run:
        print("[main] DRY_RUN モードです。Discord送信・Gemini呼び出し・状態保存は行いません")

    config = load_config()
    now = datetime.now(JST)
    date_str = now.strftime("%Y-%m-%d")
    print(f"[main] 実行日時: {now.isoformat()}")

    # 0. 天気(ニュースより先に1通送る)。失敗してもニュースは続行する。
    run_weather(config, date_str, dry_run)

    # 1. RSS 取得
    articles = fetch_feeds.fetch_all(config, now=now)
    if not articles:
        # 全フィードが落ちている状況。通知する中身が無いのでここで終える。
        print("[main] 記事を1件も取得できませんでした。処理を終了します")
        return 1

    # 2. 名寄せ
    clusters = cluster_mod.cluster_articles(
        articles,
        config["cluster_threshold"],
        min_shared_bigrams=config.get(
            "min_shared_bigrams", cluster_mod.MIN_SHARED_BIGRAMS
        ),
    )

    # 3. はてブ件数(失敗しても続行する)
    try:
        attach_hatena_counts(clusters)
    except Exception as e:
        print(f"[main] はてブ件数の取得に失敗しました(スコアの当該項は0として続行): {e}")

    # 4. スコアリングと選抜
    seen = rank.load_seen(SEEN_PATH)
    selected = rank.rank_and_select(clusters, config, seen)
    chosen = [c for lane_id in selected for c in selected[lane_id]]
    print(f"[main] 選抜結果: 合計{len(chosen)}件")

    if not chosen:
        if config.get("notify_when_empty", False):
            notify_discord.notify_empty(os.environ, date_str, dry_run=dry_run)
        else:
            print("[main] 通知対象が0件で、notify_when_empty が false のため何も送りません")
        return 0

    # 5. 要約(失敗しても続行する。要約なしなら見出しとリンクだけで通知する)
    try:
        n = summarize.summarize_clusters(
            chosen,
            api_key=os.environ.get("GEMINI_API_KEY", ""),
            model=os.environ.get("GEMINI_MODEL") or config.get("gemini_model", summarize.DEFAULT_MODEL),
            max_chars=config.get("summary_max_chars", 120),
            dry_run=dry_run,
        )
        print(f"[main] {n}件の要約を生成しました")
    except Exception as e:
        print(f"[main] 要約に失敗しました(要約なしで通知を続行します): {e}")

    # 6. Discord 通知
    sent = notify_discord.notify(
        selected, config["lanes"], os.environ, date_str, dry_run=dry_run
    )

    # 7. 既報の記録。
    #    DRY_RUN では保存しない(記録してしまうと、本番実行したときに
    #    「既報」として弾かれて何も通知されなくなるため)。
    #    送信が1件も成功しなかった場合も記録しない(次回に持ち越す)。
    if dry_run:
        print("[main] DRY_RUN のため既報の記録は行いません")
    elif sent == 0:
        print("[main] 送信できたメッセージが無いため、既報の記録は行いません")
    else:
        rank.mark_seen(seen, chosen, now.date())
        rank.save_seen(SEEN_PATH, seen, config["seen_ttl_days"], now.date())

    return 0


if __name__ == "__main__":
    sys.exit(main())
