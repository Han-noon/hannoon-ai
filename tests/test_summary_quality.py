from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from collector.storage import ensure_sqlite_db, save_article_analysis_result
from collector.article_llm import ArticleLLMAnalyzer
from db import events, topics
from event_classifier.prompts import build_event_summary_prompt
from event_summary import generate_event_summary, generate_event_title
from openai_client.client import parse_json_object
from summary_utils import TOPIC_TITLE_MAX_CHARS, normalize_summary, normalize_topic_title


class FakeConn:
    def __init__(self):
        self.query_params = None
        self.execute_params = None

    def query_one(self, sql, params=None):
        self.query_params = params
        return {"id": 123}

    def execute(self, sql, params=None):
        self.execute_params = params


class FakeLLM:
    """결과 하나 또는 호출 순서대로 돌려줄 결과 목록을 받는다(마지막 결과는 반복)."""

    def __init__(self, *results):
        self.results = list(results)
        self.calls = 0

    def request_json(self, prompt, **kwargs):
        result = self.results[min(self.calls, len(self.results) - 1)]
        self.calls += 1
        if isinstance(result, Exception):
            raise result
        missing = kwargs.get("required_keys", set()) - set(result)
        if missing:
            raise ValueError(f"missing {missing}")
        return result


class SummaryQualityTests(unittest.TestCase):
    def test_normalize_summary_limits_sentences_and_chars(self):
        text = "One. Two. Three. Four. Five. " + ("Tail text. " * 200)

        summary = normalize_summary(text, max_chars=80)

        self.assertLessEqual(len(summary), 80)
        self.assertNotIn("Five", summary)

    def test_normalize_topic_title_limits_chars_and_trailing_period(self):
        title = (
            "김승룡 소방청장이 외유성 출장 의혹 등으로 감찰을 받아 "
            "15일자로 의원 면직 처리됐다."
        )

        normalized = normalize_topic_title(title)

        self.assertLessEqual(len(normalized), TOPIC_TITLE_MAX_CHARS)
        self.assertFalse(normalized.endswith("."))

    def test_parse_json_object_accepts_control_characters(self):
        data = parse_json_object('{"summary": "line one\nline two"}')

        self.assertEqual(data["summary"], "line one\nline two")

    def test_article_result_save_normalizes_summary(self):
        long_summary = "One. Two. Three. Four. Five. " + ("Tail text. " * 200)

        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = str(Path(tmp_dir) / "news.db")
            with ensure_sqlite_db(db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO articles (id, link, guid, title, status)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (1, "https://example.com/a", "guid-a", "title", "ready"),
                )
                save_article_analysis_result(
                    conn,
                    article_id=1,
                    summary=long_summary,
                    abuse_score=0.0,
                    abuse_label="normal",
                    keywords=["keyword"],
                )

                row = conn.query_one(
                    "SELECT summary FROM article_ai_results WHERE article_id = ?",
                    (1,),
                )

        self.assertLessEqual(len(row["summary"]), 700)
        self.assertNotIn("Five", row["summary"])

    def test_event_and_topic_repositories_normalize_summary_writes(self):
        long_summary = "One. Two. Three. Four. Five. " + ("Tail text. " * 200)

        event_conn = FakeConn()
        events.create_new_event(
            event_conn,
            category="사회",
            title="event title",
            summary=long_summary,
            core_content="core",
            embedding_text="embedding text",
            embedding_literal="[0.1]",
        )
        self.assertLessEqual(len(event_conn.query_params[2]), 700)
        self.assertNotIn("Four", event_conn.query_params[2])

        events.update_event_summary(event_conn, 123, long_summary, "짧은 요약")
        self.assertEqual(event_conn.execute_params[0], "One. Two. Three.")
        self.assertEqual(event_conn.execute_params[1:], ("짧은 요약", 123))
        events.update_event_summary(event_conn, 123, long_summary)
        self.assertIsNone(event_conn.execute_params[1])

        topic_conn = FakeConn()
        long_title = (
            "서울 송파경찰서는 잠실 개표소에서 핸드볼 여성 유소년 "
            "국가대표팀 선수들의 소지품을 검사한 피의자의 출석을 요구했다."
        )
        topics.create_topic(topic_conn, "사회", long_title, long_summary)
        self.assertLessEqual(len(topic_conn.query_params[1]), TOPIC_TITLE_MAX_CHARS)
        self.assertLessEqual(len(topic_conn.query_params[2]), 700)
        self.assertNotIn("Five", topic_conn.query_params[2])

        topics.update_topic(topic_conn, 123, long_title, long_summary)
        self.assertLessEqual(len(topic_conn.execute_params[0]), TOPIC_TITLE_MAX_CHARS)
        self.assertLessEqual(len(topic_conn.execute_params[1]), 700)

    def test_generate_event_title_normalizes_and_falls_back_to_headline(self):
        title = generate_event_title(
            FakeLLM({"title": "서울 아파트값 30주 연속 상승."}),
            article_title="“또 올랐다”…서울 아파트값",
            major="major",
            published_at="2026-07-01",
        )
        self.assertEqual(title, "서울 아파트값 30주 연속 상승")

        for client in (FakeLLM(RuntimeError("boom")), FakeLLM({"title": " "})):
            title = generate_event_title(
                client,
                article_title="  “또 올랐다”…서울   아파트값 ",
                major="major",
                published_at="2026-07-01",
            )
            self.assertEqual(title, "“또 올랐다”…서울 아파트값")

    def test_generate_event_title_never_cuts_long_title(self):
        long_title = "이재명 대통령 NATO 정상회의 참석 후 몽골 국빈 방문 및 한몽 공동선언 발표 일정 확정."
        client = FakeLLM({"title": long_title})
        title = generate_event_title(client, article_title="h", major="m", published_at="2026-07-07")
        self.assertEqual(client.calls, 1)
        self.assertEqual(title, long_title.rstrip("."))

    def test_event_summary_prompt_gives_publish_times_and_latest_base_time(self):
        prompt = build_event_summary_prompt(
            "t",
            [
                {"title": "a", "published_at": "2026-07-06 06:35", "summary": "A."},
                {"title": "b", "published_at": "2026-07-06 19:44", "summary": "B."},
            ],
        )
        self.assertIn("요약 기준 시점(가장 최근 기사 게시 시각): 2026-07-06 19:44", prompt)
        self.assertIn("게시 시각: 2026-07-06 06:35", prompt)

    def test_generate_event_summary_returns_short_summary(self):
        articles = [
            {"article_id": 1, "title": "a", "summary": "A."},
            {"article_id": 2, "title": "b", "summary": "B."},
        ]
        summary, short = generate_event_summary(
            FakeLLM({"summary": "One. Two. Three. Four.", "short_summary": "의대 정원 2,000명 확대 공식화."}),
            event_title="t",
            article_summaries=articles,
            fallback_summary="Fallback.",
        )
        self.assertEqual(summary, "One. Two. Three.")
        self.assertEqual(short, "의대 정원 2,000명 확대 공식화")

        summary, short = generate_event_summary(
            FakeLLM(RuntimeError("boom")),
            event_title="t",
            article_summaries=articles,
            fallback_summary="F1. F2. F3. F4.",
        )
        self.assertEqual(summary, "F1. F2. F3.")
        self.assertIsNone(short)

    def test_abuse_disabled_skips_abuse_and_keyword_classification(self):
        class FakeAnalyzer(ArticleLLMAnalyzer):
            def _summarize(self, *, title, category, content):
                return "Summary one. Summary two."

            def _classify_abuse(self, **kwargs):
                raise AssertionError("abuse classification should be skipped")

            def _extract_keywords(self, *, title, summary, content):
                raise AssertionError("keyword extraction should be skipped")

        analyzer = FakeAnalyzer(abuse_enabled=False)

        result = analyzer.analyze(
            title="title",
            subtitle="subtitle",
            category="사회",
            content="content",
        )

        self.assertEqual(result.abuse_label, "normal")
        self.assertEqual(result.abuse_score, 0.0)
        self.assertEqual(result.abuse_reason, "abuse_classification_skipped")
        self.assertEqual(result.keywords, [])


if __name__ == "__main__":
    unittest.main()
