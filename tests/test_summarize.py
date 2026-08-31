"""summarize.py(Gemini要約モジュール)のテスト。

ネットワークアクセスは一切行わない。_call_gemini_apiをモンキーパッチするか、
parse_response / build_prompt / dry_run経路だけを対象にする。
"""

import json

import pytest

import summarize
from summarize import build_prompt, parse_response, summarize_clusters


class TestParseResponse:
    def test_正常なjson配列を解析できる(self):
        text = '[{"index": 0, "summary": "要約A"}, {"index": 1, "summary": "要約B"}]'
        assert parse_response(text, count=2) == {0: "要約A", 1: "要約B"}

    def test_コードフェンス付きでも解析できる(self):
        text = '```json\n[{"index": 0, "summary": "要約A"}]\n```'
        assert parse_response(text, count=1) == {0: "要約A"}

    def test_indexが範囲外負数のものは捨てられる(self):
        text = '[{"index": -1, "summary": "負のindex"}, {"index": 0, "summary": "OK"}]'
        assert parse_response(text, count=2) == {0: "OK"}

    def test_indexがcount以上のものは捨てられる(self):
        text = '[{"index": 5, "summary": "範囲外"}, {"index": 0, "summary": "OK"}]'
        assert parse_response(text, count=2) == {0: "OK"}

    def test_summaryが空文字のものは捨てられる(self):
        text = '[{"index": 0, "summary": ""}, {"index": 1, "summary": "OK"}]'
        assert parse_response(text, count=2) == {1: "OK"}

    def test_壊れたjsonから完全なオブジェクトだけ部分救出する(self):
        text = '[{"index": 0, "summary": "1本目"}, {"index": 1, "summary": "2本目の途中で切れ'
        assert parse_response(text, count=2) == {0: "1本目"}

    def test_完全に壊れた入力は空辞書で例外にならない(self):
        assert parse_response("これはJSONではありません", count=2) == {}

    def test_空文字列は空辞書(self):
        assert parse_response("", count=2) == {}


class TestBuildPrompt:
    def test_リード文が長い場合に切り詰められる(self, make_article, make_cluster):
        long_lead = "あ" * 1000
        cluster = make_cluster([make_article("見出し", lead=long_lead)])
        prompt = build_prompt([cluster], max_chars=120)
        # summarize.LEAD_MAX_CHARS(500字)を超える分は入っていない。
        assert ("あ" * 501) not in prompt
        assert ("あ" * summarize.LEAD_MAX_CHARS) in prompt

    def test_トピック番号が0始まりで全件含まれる(self, make_article, make_cluster):
        clusters = [make_cluster([make_article(f"見出し{i}", lead=f"リード{i}")]) for i in range(3)]
        prompt = build_prompt(clusters, max_chars=120)
        for i in range(3):
            assert f"### トピック{i}" in prompt
            assert f"見出し{i}" in prompt

    def test_媒体名が列挙される(self, make_article, make_cluster):
        a1 = make_article("見出し", outlet="朝日新聞", lead="リード")
        a2 = make_article("見出し", outlet="時事通信", lead="リード")
        cluster = make_cluster([a1, a2])
        prompt = build_prompt([cluster], max_chars=120)
        assert "朝日新聞" in prompt
        assert "時事通信" in prompt


class TestSummarizeClusters:
    def test_leadを持たないクラスタはgeminiに送られない(self, monkeypatch, make_article, make_cluster):
        """見出しだけから要約を捏造させないための安全装置。プロンプトの内容そのものを検証する。"""
        no_lead = make_cluster([make_article("リード無し見出し", lead=None)])
        with_lead = make_cluster([make_article("リード有り見出し", lead="リード文です")])

        captured = {}

        def fake_call(prompt, api_key, model, max_retries=3):
            captured["prompt"] = prompt
            return json.dumps([{"index": 0, "summary": "生成された要約"}])

        monkeypatch.setattr(summarize, "_call_gemini_api", fake_call)

        count = summarize_clusters([no_lead, with_lead], api_key="dummy-key")

        assert count == 1
        assert with_lead.summary == "生成された要約"
        assert no_lead.summary is None
        assert "リード無し見出し" not in captured["prompt"]
        assert "リード有り見出し" in captured["prompt"]

    def test_対象クラスタが0件なら何もせず0を返す(self, monkeypatch, make_article, make_cluster):
        no_lead = make_cluster([make_article("見出し", lead=None)])

        def fake_call(*args, **kwargs):
            raise AssertionError("対象が0件ならAPIを呼んではいけない")

        monkeypatch.setattr(summarize, "_call_gemini_api", fake_call)

        count = summarize_clusters([no_lead], api_key="dummy-key")
        assert count == 0
        assert no_lead.summary is None

    def test_api_keyが空なら0を返しsummaryを書き換えない(self, monkeypatch, make_article, make_cluster):
        cluster = make_cluster([make_article("見出し", lead="リード")])

        def fake_call(*args, **kwargs):
            raise AssertionError("api_keyが空ならAPIを呼んではいけない")

        monkeypatch.setattr(summarize, "_call_gemini_api", fake_call)

        count = summarize_clusters([cluster], api_key="")
        assert count == 0
        assert cluster.summary is None

    def test_dry_runならapiを呼ばずsummaryを書き換えない(self, monkeypatch, make_article, make_cluster):
        cluster = make_cluster([make_article("見出し", lead="リード")])

        def fake_call(*args, **kwargs):
            raise AssertionError("dry_run中はAPIを呼んではいけない")

        monkeypatch.setattr(summarize, "_call_gemini_api", fake_call)

        count = summarize_clusters([cluster], api_key="dummy-key", dry_run=True)
        assert count == 0
        assert cluster.summary is None

    def test_apiが例外を投げてもsummarize_clustersが例外を伝播させない(self, monkeypatch, make_article, make_cluster):
        """モジュールdocstring([summarize.py]の冒頭)が謳う「例外は投げない」契約の検証。"""
        cluster = make_cluster([make_article("見出し", lead="リード")])

        def fake_call(*args, **kwargs):
            raise RuntimeError("ネットワークエラー")

        monkeypatch.setattr(summarize, "_call_gemini_api", fake_call)

        count = summarize_clusters([cluster], api_key="dummy-key")

        assert count == 0
        assert cluster.summary is None

    def test_応答が壊れたjsonでも例外を投げず0を返す(self, monkeypatch, make_article, make_cluster):
        cluster = make_cluster([make_article("見出し", lead="リード")])

        def fake_call(*args, **kwargs):
            return "これはJSONではありません"

        monkeypatch.setattr(summarize, "_call_gemini_api", fake_call)

        count = summarize_clusters([cluster], api_key="dummy-key")
        assert count == 0
        assert cluster.summary is None

    def test_長すぎる要約はmax_charsで切り詰められて末尾に三点リーダが付く(
        self, monkeypatch, make_article, make_cluster
    ):
        cluster = make_cluster([make_article("見出し", lead="リード")])
        too_long_summary = "あ" * 200  # max_chars(10)の1.5倍(15)を超える

        def fake_call(*args, **kwargs):
            return json.dumps([{"index": 0, "summary": too_long_summary}])

        monkeypatch.setattr(summarize, "_call_gemini_api", fake_call)

        count = summarize_clusters([cluster], api_key="dummy-key", max_chars=10)

        assert count == 1
        assert cluster.summary == "あ" * 10 + "…"
