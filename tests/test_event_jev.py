from __future__ import annotations

from pathlib import Path
import os
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from event_classifier import jev


CANDIDATES = [
    {"id": 11, "title": "a", "core_content": "x1", "source_core_content": "y1", "source_published_at": "2026-07-01 09:00:00"},
    {"id": 22, "title": "b", "core_content": "x2", "source_core_content": "y2", "source_published_at": "2026-07-02 09:00:00"},
    {"id": 33, "title": "c", "core_content": "x3", "source_core_content": "y3", "source_published_at": "2026-07-03 09:00:00"},
]


def fixed(*probs):
    return lambda state, questions: {f"cand_{i}": p for i, p in enumerate(probs, 1)}


class FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = ""

    def json(self):
        return self._payload


class JevDecisionTest(unittest.TestCase):
    def test_state_lists_candidates_with_member_date_only(self):
        state = jev.build_state("T", "2026-07-05", "M", CANDIDATES)
        self.assertIn("[후보 이벤트 3]", state)
        self.assertIn("게시일 2026-07-02)", state)
        self.assertEqual(list(jev.build_questions(3)), ["cand_1", "cand_2", "cand_3"])

    def test_assigns_to_highest_probability_candidate(self):
        decision, info = jev.decide_assignment("T", "d", "M", CANDIDATES, 0.30, fixed(0.2, 0.8, 0.6))
        self.assertEqual(decision["action"], "assign")
        self.assertEqual(decision["event_id"], 22)
        self.assertEqual(info["best_index"], 1)
        self.assertEqual(info["probs"], [0.2, 0.8, 0.6])

    def test_creates_with_article_title_below_threshold(self):
        decision, _ = jev.decide_assignment("새 제목", "d", "M", CANDIDATES, 0.30, fixed(0.29, 0.1, 0.0))
        self.assertEqual(decision["action"], "create")
        self.assertEqual(decision["event_title"], "새 제목")

    def test_threshold_is_inclusive_and_tie_prefers_search_rank(self):
        decision, _ = jev.decide_assignment("T", "d", "M", CANDIDATES, 0.30, fixed(0.30, 0.30, 0.1))
        self.assertEqual(decision["action"], "assign")
        self.assertEqual(decision["event_id"], 11)

    def test_missing_probability_raises_instead_of_creating(self):
        with self.assertRaises(RuntimeError):
            jev.decide_assignment("T", "d", "M", CANDIDATES, 0.30, fixed(0.9))

    def test_empty_candidates_raise(self):
        with self.assertRaises(ValueError):
            jev.decide_assignment("T", "d", "M", [], 0.30, fixed())


class JevRequestTest(unittest.TestCase):
    def test_missing_key_raises(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError):
                jev.require_api_key()

    def test_retries_rate_limit_then_returns_probabilities(self):
        ok = FakeResponse(200, {"answers": {"cand_1": {"noul": 0.42}}})
        with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "test"}), \
                mock.patch.object(jev.requests, "post", side_effect=[FakeResponse(429), ok]) as post, \
                mock.patch.object(jev.time, "sleep"):
            self.assertEqual(jev.request_probs("s", {"cand_1": "q"}), {"cand_1": 0.42})
            self.assertEqual(post.call_count, 2)

    def test_gives_up_after_repeated_rate_limits(self):
        with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "test"}), \
                mock.patch.object(jev.requests, "post", return_value=FakeResponse(429)), \
                mock.patch.object(jev.time, "sleep"):
            with self.assertRaises(RuntimeError):
                jev.request_probs("s", {"cand_1": "q"})


if __name__ == "__main__":
    unittest.main()
