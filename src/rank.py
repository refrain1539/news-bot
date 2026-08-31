"""
スコアリングと選抜モジュール。

このボットは「自分の興味」ではなく「世間の重要度」だけで記事を選ぶ。世間の重要度は
自前で計算せず、すでに人間が付けた順位(Yahoo!トピックス採用・livedoorトピックス採用・
時事通信ランキング入り)や、報道媒体数・はてなブックマーク件数といった外形的な指標を
借りてスコア化する。

**スコアはレーン内でしか比較しない。** 総合レーンとテックレーンを1つのプールに混ぜると、
curated_yahoo / curated_livedoor の項が定義上いつも0になるテック記事は構造的に1本も
選ばれなくなる。そのため config.yml で quota をレーンごとに固定で分けており、
rank_and_select() もレーンごとに独立して候補を集め・足切りし・並べ替える。この性質を
壊す実装(全レーンを1つのリストにまとめてからまとめて上位N件を採る、など)をしないこと。

既報の判定は URL の正規化(normalize_url)を介して行う。同じ記事でも rss/ref/utm_* などの
トラッキングパラメータが付くと別 URL に見えてしまうため、比較前に正規化する。
"""

import json
import math
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from models import Article, Cluster

# 既報判定・クエリ正規化で除去するパラメータ名(完全一致)。
# 小文字化してから比較するため、大文字表記が混じっていても除去できる。
_DROP_PARAMS_EXACT = {"source", "ref", "fbclid", "gclid"}
# 前方一致で除去するパラメータの接頭辞。
_DROP_PARAMS_PREFIX = ("utm_", "cx_")


def normalize_url(url: str) -> str:
    """
    既報判定のために URL を正規化する。

    - scheme / host を小文字化し、http は https に寄せる(サイト側のリダイレクトで
      揺れるだけで別記事扱いにしないため)。
    - host 先頭の www. を除去する。
    - トラッキング用のクエリパラメータ(utm_*, source, ref, cx_*, fbclid, gclid)を
      除去し、残ったパラメータはキー昇順に並べ直す(パラメータの順序が入れ替わっただけで
      別 URL 扱いにならないようにするため)。
    - fragment(#...)を除去する。
    - パス末尾のスラッシュを除去する(パスが "/" だけの場合は残す)。

    パースできない入力でも例外は投げず、小文字化しただけの文字列を返す。
    """
    try:
        parsed = urlparse(url)

        scheme = parsed.scheme.lower()
        if scheme == "http":
            scheme = "https"

        netloc = parsed.netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[len("www."):]

        path = parsed.path
        if len(path) > 1 and path.endswith("/"):
            path = path.rstrip("/") or "/"

        kept_params = []
        for key, value in parse_qsl(parsed.query, keep_blank_values=True):
            key_lower = key.lower()
            if key_lower in _DROP_PARAMS_EXACT:
                continue
            if key_lower.startswith(_DROP_PARAMS_PREFIX):
                continue
            kept_params.append((key, value))
        kept_params.sort(key=lambda kv: kv[0])
        query = urlencode(kept_params)

        return urlunparse((scheme, netloc, path, "", query, ""))
    except Exception:
        try:
            return url.lower()
        except Exception:
            return str(url).lower()


def score_cluster(cluster: Cluster, weights: dict) -> tuple[float, dict]:
    """
    クラスタのスコアと内訳を計算する。

    内訳(breakdown)には寄与が0でない項だけを入れる。ログで「なぜこのスコアに
    なったか」を一目で追えるようにするため。
    """
    score = 0.0
    breakdown: dict[str, float] = {}

    # 1. シグナル項(curated_yahoo, curated_livedoor, jiji_ranking など)。
    #    outlet_count と hatena はここでは扱わない特別な重みなので除外する。
    for name, weight in weights.items():
        if name in ("outlet_count", "hatena"):
            continue
        if cluster.has_signal(name):
            score += weight
            breakdown[name] = round(weight, 3)

    # 2. 報道媒体数(何社が報じたか)。1社だけなら寄与0。
    outlet_weight = weights.get("outlet_count", 0.0)
    outlet_contribution = outlet_weight * (cluster.outlet_count - 1)
    if outlet_contribution != 0:
        score += outlet_contribution
        breakdown["outlet_count"] = round(outlet_contribution, 3)

    # 3. はてなブックマーク件数。件数そのものではなく桁で効かせる。
    hatena_weight = weights.get("hatena", 0.0)
    hatena_contribution = hatena_weight * math.log10(cluster.hatena_count + 1)
    if hatena_contribution != 0:
        score += hatena_contribution
        breakdown["hatena"] = round(hatena_contribution, 3)

    return score, breakdown


def load_seen(path: str) -> dict[str, str]:
    """
    既報 URL の記録({正規化URL: "YYYY-MM-DD"})を読み込む。

    ファイルが無い場合・壊れている場合は空の辞書を返す。既報記録は既報の
    通知を抑制するためだけの補助的な情報であり、読み込みに失敗したからと
    いって全体を止めてはいけない。
    """
    p = Path(path)
    if not p.exists():
        print(f"[rank] 既報ファイルが見つかりません({path})。既報無しとして扱います")
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("トップレベルが辞書ではありません")
        return {str(k): str(v) for k, v in data.items()}
    except Exception as e:
        print(f"[rank] 既報ファイルの読み込みに失敗しました({path}): {e}。既報無しとして扱います")
        return {}


def save_seen(path: str, seen: dict[str, str], ttl_days: int, today: date) -> None:
    """
    既報 URL の記録を TTL で間引いてから JSON で保存する。

    ttl_days より古いエントリ・日付として読めないエントリは保存前に削除する
    (ファイルが際限なく肥大化しないようにするため)。キー昇順で保存するのは
    git 管理下に置いたときの差分を読みやすくするため。
    """
    cutoff = today - timedelta(days=ttl_days)
    cleaned: dict[str, str] = {}
    for url, seen_date_str in seen.items():
        try:
            seen_date = date.fromisoformat(seen_date_str)
        except (TypeError, ValueError):
            continue
        if seen_date >= cutoff:
            cleaned[url] = seen_date_str

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(dict(sorted(cleaned.items())), f, ensure_ascii=False, indent=2)

    print(f"[rank] 既報を{len(cleaned)}件保存しました(TTL {ttl_days}日, {path})")


def mark_seen(seen: dict[str, str], clusters: list[Cluster], today: date) -> None:
    """
    通知したクラスタの全 URL(代表記事だけでなく cluster.urls の全て)を
    既報として記録する。代表記事は実行日によって入れ替わりうるため、
    代表の URL だけを記録すると同じ話題を翌日また通知してしまう。
    """
    today_str = today.isoformat()
    for cluster in clusters:
        for url in cluster.urls:
            seen[normalize_url(url)] = today_str


def _sort_key(cluster: Cluster):
    """
    レーン内の並べ替えキー。スコア降順 → リード文あり優先 → 媒体数降順 →
    代表記事の published 昇順(None は最後)→ 正規化 URL 昇順、で完全に決定的にする。

    リード文の有無をスコアの項ではなく同点時の tiebreaker に置いているのは、
    「要約が付くこと」より「世間の重要度」を優先するため。スコアに加点すると、
    重要度の低い記事がリード文を持つというだけで重要なニュースを押しのける。
    同点のときだけ要約を作れるほうを選ぶ、という強さがちょうどよい。
    """
    published = cluster.representative.published
    published_key = (published is None, published)
    return (
        -cluster.score,
        0 if cluster.lead else 1,
        -cluster.outlet_count,
        published_key,
        normalize_url(cluster.url),
    )


def rank_and_select(clusters, config, seen) -> dict[str, list[Cluster]]:
    """
    既報を除外し、レーンごとにスコアリング・足切り・並べ替えを行って
    quota 件を選抜する。

    戻り値は {lane_id: [Cluster, ...]} で、config の lanes に登場する
    全レーン id を必ずキーとして含む(0件でも空リストを入れる)。
    """
    lanes_config = config["lanes"]
    spill_over = bool(config.get("spill_over", False))
    lane_ids = [lane["id"] for lane in lanes_config]

    # 1. 既報と「リード文の供給専用」クラスタを除外する。
    #    lead_only の記事だけで構成されたクラスタは、他のソースが誰も
    #    取り上げていない話題(例: トピックスに入っていない個別のスポーツ記事)
    #    なので通知の候補にしない。lead_only の記事が混じっていても、
    #    通常のソースの記事が1本でもあれば候補として残す。
    #    require_lead が true の場合は、リード文が取れなかったトピックも
    #    ここで落とす。要約を作れないトピックは見出しとリンクだけの通知に
    #    なるが、それは読まれないため通知する意味が薄い、という運用方針。
    require_lead = bool(config.get("require_lead", False))

    fresh_clusters = []
    dropped_lead_only = 0
    dropped_no_lead = 0
    for cluster in clusters:
        if all(a.lead_only for a in cluster.articles):
            dropped_lead_only += 1
            continue
        if require_lead and not cluster.lead:
            dropped_no_lead += 1
            continue
        normalized_urls = [normalize_url(u) for u in cluster.urls]
        if any(u in seen for u in normalized_urls):
            continue
        fresh_clusters.append(cluster)

    if dropped_lead_only:
        print(
            f"[rank] リード文供給専用のトピックを{dropped_lead_only}件、候補から外しました"
        )
    if dropped_no_lead:
        print(
            f"[rank] リード文が取れず要約を作れないトピックを{dropped_no_lead}件、"
            f"候補から外しました(require_lead: true)"
        )

    # 2〜4. レーンごとにスコアを計算し、min_score で足切りしてから並べ替える。
    #        レーンをまたいでスコアを比較することは絶対にしない。
    lane_sorted_candidates: dict[str, list[Cluster]] = {}
    for lane in lanes_config:
        lane_id = lane["id"]
        weights = lane.get("weights", {})
        min_score = lane.get("min_score", 0.0)

        lane_clusters = [c for c in fresh_clusters if c.lane == lane_id]
        for cluster in lane_clusters:
            score, breakdown = score_cluster(cluster, weights)
            cluster.score = score
            cluster.breakdown = breakdown

        passed = [c for c in lane_clusters if c.score >= min_score]
        passed.sort(key=_sort_key)
        lane_sorted_candidates[lane_id] = passed

    # 5. 上位 quota 件を採用し、残りは次点として取っておく。
    selected: dict[str, list[Cluster]] = {}
    leftover: dict[str, list[Cluster]] = {}
    for lane in lanes_config:
        lane_id = lane["id"]
        quota = lane["quota"]
        passed = lane_sorted_candidates[lane_id]
        selected[lane_id] = passed[:quota]
        leftover[lane_id] = passed[quota:]

    # 6. spill_over: quota を満たせなかったレーンがある分だけ、全レーンの次点
    #    プールをスコア降順でまとめて埋める。ただし各記事は「本来のレーン」の
    #    結果リストに戻す(レーンを越えて記事を混ぜるわけではなく、あるレーンの
    #    未消化分をもう一方のレーン自身の次点で使わせるイメージ)。
    #    これによりレーンが3つ以上あっても一般的に動く。
    if spill_over:
        total_deficit = 0
        for lane in lanes_config:
            lane_id = lane["id"]
            quota = lane["quota"]
            total_deficit += max(0, quota - len(selected[lane_id]))

        if total_deficit > 0:
            pool = []
            for lane_id in lane_ids:
                pool.extend(leftover[lane_id])
            pool.sort(key=_sort_key)

            for cluster in pool[:total_deficit]:
                selected[cluster.lane].append(cluster)

    # 7. ログ出力(dry-run での検証用)。
    label_by_id = {lane["id"]: lane.get("label", lane["id"]) for lane in lanes_config}
    for lane in lanes_config:
        lane_id = lane["id"]
        label = label_by_id[lane_id]
        candidates = lane_sorted_candidates[lane_id]
        chosen = selected[lane_id]
        max_score = chosen[0].score if chosen else 0.0
        print(
            f"[rank] {label}: 候補{len(candidates)}件 → "
            f"{len(chosen)}件を選抜(最高スコア {max_score:.2f})"
        )
        for cluster in chosen:
            print(
                f"[rank]   ・{cluster.title} "
                f"score={cluster.score:.2f} breakdown={cluster.breakdown}"
            )

    # 戻り値は config に登場する全レーン id を必ず含める。
    return {lane_id: selected.get(lane_id, []) for lane_id in lane_ids}
