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
from openai_client.client import parse_json_object
from summary_utils import normalize_summary


class FakeConn:
    def __init__(self):
        self.query_params = None
        self.execute_params = None

    def query_one(self, sql, params=None):
        self.query_params = params
        return {"id": 123}

    def execute(self, sql, params=None):
        self.execute_params = params


class SummaryQualityTests(unittest.TestCase):
    def test_normalize_summary_limits_sentences_and_chars(self):
        text = "One. Two. Three. Four. Five. " + ("Tail text. " * 200)

        summary = normalize_summary(text, max_chars=80)

        self.assertLessEqual(len(summary), 80)
        self.assertNotIn("Five", summary)

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
        self.assertNotIn("Five", event_conn.query_params[2])

        topic_conn = FakeConn()
        topics.create_topic(topic_conn, "사회", "topic title", long_summary)
        self.assertLessEqual(len(topic_conn.query_params[2]), 700)
        self.assertNotIn("Five", topic_conn.query_params[2])

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
