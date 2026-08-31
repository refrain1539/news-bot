"""
Gemini APIを使って、ニュースクラスタの日本語要約を生成するモジュール。

- REST APIを直接叩く (https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent)
- リード文を持つクラスタだけをまとめて1リクエストで要約する(無料枠のRPD上限を
  消費しないための措置。1日1回・10件程度をまとめて処理する想定)
- リード文が無いクラスタ(見出ししか情報が無い)はGeminiに渡さない。見出しだけから
  要約を書かせると内容を捏造するため、summaryはNoneのままにし、通知側は
  見出しとリンクだけで表示する
- 応答は Gemini の構造化出力(responseMimeType + responseSchema)で受け取り、構文的に
  正しいJSONを保証させる。モデルがスキーマを受け付けない場合(400)はスキーマなしで
  1度だけ再試行する(judge_translate.pyと同じ流儀)
- 429・5xx(サーバ側の一時的な障害)は指数バックオフ(1秒→2秒→4秒、最大3回)で再試行する
- それでもJSONが壊れている場合に備えて保険を持つ:
  コードフェンスの除去 → 素のパース → 不正エスケープの修復 →
  波括弧の対応を数えて1オブジェクトずつ救出、の順に段階的にフォールバックする
  (judge_translate.pyの_extract_json_array / salvage_json_objectsに相当)
- 上記すべてが失敗しても例外は投げない。printでログを残し、summaryはNoneのまま
  処理を続ける。要約が無くても見出しとリンクだけで通知する価値はあるため
"""

import json
import re
import time

import requests

from models import Cluster

GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
DEFAULT_MODEL = "gemini-3.6-flash"
MAX_RETRIES = 3

# プロンプトに載せるリード文の切り詰め長。長すぎるリード文はここで打ち切る。
LEAD_MAX_CHARS = 500

# Gemini の構造化出力(responseSchema)。OpenAPI のサブセットで型名は大文字。
RESPONSE_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "index": {"type": "INTEGER"},
            "summary": {"type": "STRING"},
        },
        "required": ["index", "summary"],
    },
}


def _redact(value, api_key):
    """ログ出力用に、文字列表現から API キーを伏せる。

    requests の例外メッセージにはリクエスト URL がそのまま入ることがあり、
    このモジュールはキーを ?key= のクエリに載せているため、素直に出力すると
    GitHub Actions のログにキーが残ってしまう。
    """
    text = str(value)
    if api_key:
        text = text.replace(api_key, "***")
    return text


def build_prompt(clusters: list[Cluster], max_chars: int) -> str:
    """
    リード文ありのクラスタ(絞り込み済みの前提)から、番号付きの要約依頼プロンプトを作る。

    各トピックについて index(0始まり)・見出し・報じた媒体・リード文(500字に切り詰め)を
    与え、リード文の言い換え(コピー禁止)・事実の捏造禁止を明示する。
    """
    entries = []
    for i, cluster in enumerate(clusters):
        outlets = ", ".join(cluster.outlets)
        lead = cluster.lead or ""
        if len(lead) > LEAD_MAX_CHARS:
            lead = lead[:LEAD_MAX_CHARS]
        entries.append(
            f"### トピック{i}\n"
            f"見出し: {cluster.title}\n"
            f"報じた媒体: {outlets}\n"
            f"リード文: {lead}\n"
        )
    topics_block = "\n".join(entries)
    n = len(clusters)

    return f"""あなたは日本語ニュースの編集者です。以下の{n}件のニュース記事について、
それぞれ日本語で短い要約を書いてください。

# 対象トピック({n}件)
{topics_block}

# 指示
1. 1トピックあたり最大{max_chars}字程度・2文以内で要約せよ。
2. リード文の文言をそのまま写さず、自分の言葉で書き直せ(著作権への配慮のため、
   コピー&ペーストは厳禁)。
3. リード文に書かれていない事実を足すな。推測や背景解説を加えるな。
4. 「〜と報じられました」のような伝聞の枕詞は不要。事実を端的に書け。
5. 見出しの繰り返しにならないよう、見出しに無い情報を優先して入れよ。
6. 出力は以下のJSON配列の形式のみとし、説明文やコードフェンス(```)は不要とする。
   配列の要素数は必ずトピック件数({n}件)と一致させ、"index"にはトピック番号
   (0始まり。トピック0なら0)を入れること。全てのトピック番号を過不足なく1回ずつ含めよ。

[{{"index": <トピック番号(0始まり)>, "summary": "<{max_chars}字程度の日本語要約>"}}, ...]
"""


# JSONとして不正なエスケープ: \ の直後が許容文字でない、または \u の直後が16進4桁でない
_INVALID_ESCAPE_RE = re.compile(r'\\(?:u(?![0-9a-fA-F]{4})|(?!["\\/bfnrtu]))')


def _repair_json_escapes(text):
    """JSON文字列中の不正なエスケープをリテラルなバックスラッシュに直す。"""
    return _INVALID_ESCAPE_RE.sub(r"\\\\", text)


def _salvage_json_objects(text):
    """
    配列全体のパースに失敗したときの最終防衛線。波括弧の対応を数えて1オブジェクトずつ
    取り出し、パースできたものだけを返す。1トピック分が壊れていても残りを救う。
    """
    objects = []
    depth = 0
    start = None
    in_string = False
    escaped = False

    for i, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start is not None:
                chunk = text[start : i + 1]
                for candidate in (chunk, _repair_json_escapes(chunk)):
                    try:
                        objects.append(json.loads(candidate))
                        break
                    except json.JSONDecodeError:
                        continue
                start = None

    return objects


def _extract_json_array(text):
    """
    Gemini応答から JSON配列 部分を取り出してパースする。```json フェンス付きにも対応。
    素のパースに失敗した場合は、不正エスケープの修復 → 1オブジェクトずつの救出、
    の順に段階的にフォールバックする。すべて失敗した場合は None を返す(例外は投げない)。
    """
    text = text.strip()
    fence_match = re.search(r"```(?:json)?\s*(\[.*\])\s*```", text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1)
    else:
        first = text.find("[")
        last = text.rfind("]")
        if first != -1 and last != -1:
            text = text[first : last + 1]

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    try:
        parsed = json.loads(_repair_json_escapes(text))
        print("[summarize] 不正なエスケープを修復してパースしました")
        return parsed
    except json.JSONDecodeError:
        pass

    salvaged = _salvage_json_objects(text)
    if salvaged:
        print(f"[summarize] 配列の復元に失敗しましたが、{len(salvaged)}件を個別に救出しました")
        return salvaged

    print("[summarize] Gemini応答からJSON配列を復元できませんでした")
    return None


def parse_response(text: str, count: int) -> dict[int, str]:
    """
    Gemini応答(JSON配列)を解析し、{トピック番号(0始まり): 要約} を返す。

    index が範囲外(0未満・count以上)の要素、summary が空文字の要素は捨てる。
    何も取れなければ空辞書を返す。例外は投げない。
    """
    if not text:
        return {}

    array = _extract_json_array(text)
    if not array:
        return {}

    results = {}
    for item in array:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("index"))
        except (TypeError, ValueError):
            continue
        if idx < 0 or idx >= count:
            continue
        summary = str(item.get("summary", "") or "").strip()
        if not summary:
            continue
        results[idx] = summary

    return results


def _build_payload(prompt, with_schema):
    generation_config = {"temperature": 0.2}
    if with_schema:
        generation_config["responseMimeType"] = "application/json"
        generation_config["responseSchema"] = RESPONSE_SCHEMA
    return {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": generation_config,
    }


def _call_gemini_api(prompt, api_key, model, max_retries=MAX_RETRIES):
    """
    Gemini APIを呼び出し、応答テキストを返す。失敗時はNoneを返す。

    構造化出力(responseSchema)付きで投げ、モデルが受け付けない場合(400)は
    スキーマなしにフォールバックして1度だけ再試行する。429・5xxは指数バックオフ
    (1秒→2秒→4秒、最大3回)で再試行する。APIキーはURLのクエリに載るため、
    ログ(例外メッセージやレスポンス本文のprint)にURLをそのまま出さない。
    """
    url = f"{GEMINI_API_BASE}/{model}:generateContent?key={api_key}"
    with_schema = True

    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(url, json=_build_payload(prompt, with_schema), timeout=120)

            if resp.status_code == 429 or resp.status_code >= 500:
                wait = 2 ** (attempt - 1)
                print(
                    f"[summarize] Gemini {resp.status_code}(レート制限/サーバエラー)。"
                    f"{wait}秒待って再試行します ({attempt}/{max_retries})"
                )
                time.sleep(wait)
                continue

            if resp.status_code == 400 and with_schema:
                print(
                    "[summarize] responseSchema が拒否されました(400)。"
                    f"スキーマなしで再試行します: {resp.text[:200]}"
                )
                with_schema = False
                continue

            resp.raise_for_status()
            data = resp.json()
            return data["candidates"][0]["content"]["parts"][0]["text"]
        except Exception as e:
            print(f"[summarize] Gemini API呼び出し失敗 (試行{attempt}/{max_retries}): {e}")
            wait = 2 ** (attempt - 1)
            time.sleep(wait)

    return None


def summarize_clusters(clusters, api_key, model=DEFAULT_MODEL, max_chars=120, dry_run=False) -> int:
    """
    リード文を持つクラスタだけを対象に、Gemini へ1回のリクエストで要約を依頼し、
    各 cluster.summary に書き込む。

    - リード文の無いクラスタ(cluster.lead が None/空文字)は対象外。見出ししか
      材料が無いトピックをGeminiに渡すと内容を捏造するため。
    - 対象が0件、または api_key が空/None の場合は何もせず0を返す(要約なしでも
      通知パイプラインは継続できる)。
    - dry_run=True の場合はAPIを呼ばず、プロンプト先頭800字と対象件数をprintして
      0を返す。summaryへの書き込みは行わない。
    - max_chars の1.5倍を超える要約は、max_chars で切り詰めて末尾に"…"を付ける。
    - 失敗しても例外は投げない。summaryはNoneのまま残り、見出しとリンクだけで
      通知される。
    - 戻り値は要約を書き込めたクラスタ数。
    """
    targets = [c for c in clusters if c.lead]
    if not targets:
        return 0

    if not api_key:
        print("[summarize] APIキーが設定されていないため、要約をスキップします")
        return 0

    prompt = build_prompt(targets, max_chars)

    if dry_run:
        print(f"[summarize] dry-run: 要約対象 {len(targets)}件。プロンプト先頭800字:")
        print(prompt[:800])
        return 0

    # _call_gemini_api() は内部で通信例外を握りつぶして None を返す契約だが、
    # その契約が破れた場合(将来の変更・想定外の例外型)でもここから先へ例外を
    # 伝播させない。要約が無くても見出しとリンクだけで通知する価値があるため、
    # このモジュールは「何があっても呼び出し元を落とさない」ことを保証する。
    try:
        text = _call_gemini_api(prompt, api_key, model)
    except Exception as e:
        # 例外メッセージにはリクエスト URL(?key=... を含む)が載ることがあるため、
        # そのまま出力せず API キーを伏せてからログに残す。
        print(f"[summarize] Gemini の呼び出しに失敗しました: {_redact(e, api_key)}")
        return 0

    if text is None:
        print("[summarize] Geminiから応答が得られませんでした。要約なしで続行します")
        return 0

    try:
        summaries = parse_response(text, len(targets))
    except Exception as e:
        preview = text[:300] if text else None
        print(f"[summarize] 応答のJSONパースに失敗しました: {e} / raw: {preview}")
        return 0

    if not summaries:
        print("[summarize] 有効な要約が1件も取得できませんでした")
        return 0

    limit = int(max_chars * 1.5)
    count = 0
    for i, cluster in enumerate(targets):
        summary = summaries.get(i)
        if not summary:
            continue
        if len(summary) > limit:
            summary = summary[:max_chars].rstrip() + "…"
        cluster.summary = summary
        count += 1

    return count
