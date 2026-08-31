"""
はてなブックマーク件数取得モジュール。

はてなブックマーク API(キー不要・無料)を使用して、複数の URL に対するブックマーク件数を
一括取得する。APIの制限(1リクエストあたりのURL数が多すぎると414/400になる)を避けるため、
URL を CHUNK_SIZE 件ずつに分割して複数リクエストに分けて投げる。

通信失敗・JSON パース失敗時は例外を投げず、該当チャンクの URL を 0 件として扱い、
ログに残して処理を継続する。はてなブックマーク件数はスコア算出の一項目にすぎず、
取得不可で通知全体を止めてはいけないため。

レスポンスに含まれない URL(ブックマーク0件)は、戻り値に明示的に 0 を入れて返す。
"""

import time

import requests

CHUNK_SIZE = 50
TIMEOUT_SEC = 20

HATENA_API_URL = "https://bookmark.hatenaapis.com/count/entries"
USER_AGENT = "news-bot/1.0"


def get_counts(urls: list[str], timeout: int = TIMEOUT_SEC) -> dict[str, int]:
    """
    複数の URL に対するはてなブックマーク件数を取得する。

    Args:
        urls: ブックマーク件数を問い合わせる URL リスト
        timeout: 1リクエストあたりのタイムアウト秒数

    Returns:
        {URL: ブックマーク件数} の辞書。入力 urls に含まれる全ての URL が含まれ、
        ブックマークがない(レスポンスに含まれない) URL は 0 を返す。
    """
    if not urls:
        return {}

    # 重複を除去しながら入力順を保持。最後に戻り値では全てを含める
    unique_urls = []
    seen = set()
    for url in urls:
        if url not in seen:
            unique_urls.append(url)
            seen.add(url)

    # 全チャンクの結果を集約
    counts = {url: 0 for url in unique_urls}
    num_chunks = (len(unique_urls) + CHUNK_SIZE - 1) // CHUNK_SIZE

    # チャンク単位でリクエストを送る
    for chunk_idx in range(num_chunks):
        # チャンク間に sleep を入れる(チャンク1つだけのときは sleep しない)
        if chunk_idx > 0:
            time.sleep(0.5)

        start = chunk_idx * CHUNK_SIZE
        end = start + CHUNK_SIZE
        chunk = unique_urls[start:end]

        # 複数 URL を params に list of tuples で渡す(requests は重複キーに対応)
        params = [("url", url) for url in chunk]

        try:
            resp = requests.get(
                HATENA_API_URL,
                params=params,
                headers={"User-Agent": USER_AGENT},
                timeout=timeout,
            )
            resp.raise_for_status()
            result = resp.json()

            # レスポンスに含まれた URL のブックマーク件数を更新。
            # API が数値以外を返した場合(仕様変更や障害時)にスコア計算側で
            # 落ちないよう、ここで int に寄せておく。
            for url, count in result.items():
                if url not in counts:
                    continue
                try:
                    counts[url] = int(count)
                except (TypeError, ValueError):
                    counts[url] = 0

        except Exception as e:
            print(
                f"[hatena_count] チャンク {chunk_idx + 1}/{num_chunks} "
                f"({len(chunk)}件) の取得に失敗しました: {e}"
            )
            continue

    # ログ出力: 問い合わせ件数と、ブックマークがあった件数
    bookmarked_count = sum(1 for count in counts.values() if count > 0)
    print(
        f"[hatena_count] {len(unique_urls)}件の URL を問い合わせ、"
        f"{bookmarked_count}件にブックマークがありました"
    )

    return counts
