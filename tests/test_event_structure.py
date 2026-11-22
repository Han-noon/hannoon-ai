from __future__ import annotations

from pathlib import Path
import json
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from event_classifier import pipeline


class FakeClient:
    """프롬프트 순서대로 준비된 응답을 돌려주고, 받은 프롬프트를 기록한다."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.prompts = []
        self.kwargs = []

    def request(self, prompt, **kwargs):
        self.prompts.append(prompt)
        self.kwargs.append(kwargs)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return json.dumps(item, ensure_ascii=False)


def structure(major):
    return {"major": major, "minor": [], "entities": ["나토"]}


class FutureCompletedSentenceTest(unittest.TestCase):
    def test_flags_future_event_written_as_completed(self):
        # 실측 사례(#3159·#3004): 게시일 7/6 기사가 7/7 일을 완료형으로 씀
        for major in (
            "2026년 7월 7일부터 8일까지 튀르키예 앙카라에서 북대서양조약기구(나토) 정상회의가 개막했다.",
            "이재명 대통령은 2026년 7월 7일 튀르키예 앙카라에서 열리는 나토 정상회의에 참석하기 위해 출국했다.",
        ):
            self.assertEqual(pipeline._find_future_completed_sentences(major, "2026-07-06"), [major])

    def test_ignores_schedule_wording_past_dates_and_present_tense(self):
        for major in (
            "이란 대통령은 2026년 7월 8일 이라크를 방문할 예정이다.",
            "2026년 7월 5일 테헤란에서 장례식이 열렸다.",
            "2026년 7월 9일 안장을 앞두고 우려가 있다.",
            "정부는 2026년 7월 9일 대책을 발표한다.",
        ):
            self.assertEqual(pipeline._find_future_completed_sentences(major, "2026-07-06"), [], major)

    def test_ignores_reported_speech_and_decisions_with_future_dates(self):
        # v12 major 실측 오탐 유형: 인용·결정 연결어가 있거나 날짜를 목표로 정한 문장
        for major in (
            "삼성카드는 2026년 7월 7일부터 가맹점 대금을 정상적으로 지급하기로 결정했다.",
            "트럼프 대통령은 2026년 7월 4일 행사에서 긴 연설을 할 것이라고 발표했다.",
            "기상청은 2026년 7월 8일부터 9일 사이에 최대 150㎜의 비가 내릴 것으로 예보했다.",
            "재판부는 다음 공판준비기일을 2026년 8월 25일로 정했다.",
            "그는 법안이 2026년 10월 2일 전에 처리되어야 한다고 강조했다.",
            "특위는 2026년 7월 14일 1차 청문회를 개최하기로 하고, 97명을 증인으로 채택했다.",
        ):
            self.assertEqual(pipeline._find_future_completed_sentences(major, "2026-07-06"), [], major)

    def test_flags_completed_sentence_with_trailing_parenthesis(self):
        major = "2026년 7월 7일 나토 정상회의가 개막했다(현지시간)."
        self.assertEqual(pipeline._find_future_completed_sentences(major, "2026-07-06"), [major])

    def test_flags_only_offending_sentence(self):
        major = "홈플러스는 2026년 7월 3일 회생절차 폐지 결정을 받았다. 2026년 7월 8일 매장이 문을 닫았다."
        self.assertEqual(
            pipeline._find_future_completed_sentences(major, "2026-07-06"),
            ["2026년 7월 8일 매장이 문을 닫았다."],
        )

    def test_bad_published_date_disables_check(self):
        self.assertEqual(pipeline._find_future_completed_sentences("2026년 7월 8일 열렸다.", ""), [])


class ExtractStructureTest(unittest.TestCase):
    FLAGGED = "2026년 7월 7일 나토 정상회의가 개막했다."
    FIXED = "2026년 7월 7일 나토 정상회의가 개막할 예정이다."

    def test_no_flag_uses_single_call(self):
        client = FakeClient(structure(self.FIXED))
        result, info = pipeline._extract_structure(client, "제목. 요약", "2026-07-06")
        self.assertEqual((result["major"], info["status"], len(client.prompts)), (self.FIXED, "none", 1))

    def test_flag_triggers_one_retry_and_adopts_fixed_response(self):
        client = FakeClient(structure(self.FLAGGED), structure(self.FIXED))
        result, info = pipeline._extract_structure(client, "제목. 요약", "2026-07-06")
        self.assertEqual(result["major"], self.FIXED)
        self.assertEqual(info, {"flagged": [self.FLAGGED], "status": "fixed", "after": []})
        self.assertEqual(len(client.prompts), 2)
        self.assertIn("[재검토 요청]", client.prompts[1])
        self.assertIn(self.FLAGGED, client.prompts[1])

    def test_retry_not_improving_keeps_first_response(self):
        first = structure(self.FLAGGED)
        second = {"major": "2026년 7월 7일 나토 정상회의가 열렸다.", "minor": [], "entities": ["다른 엔티티"]}
        client = FakeClient(first, second)
        result, info = pipeline._extract_structure(client, "제목. 요약", "2026-07-06")
        self.assertEqual((result["major"], result["entities"], info["status"]), (self.FLAGGED, ["나토"], "still_flagged"))
        self.assertEqual(len(client.prompts), 2)  # 재요청은 1회뿐

    def test_failed_retry_keeps_first_response(self):
        client = FakeClient(structure(self.FLAGGED), ValueError("bad json"))
        result, info = pipeline._extract_structure(client, "제목. 요약", "2026-07-06")
        self.assertEqual((result["major"], info["status"]), (self.FLAGGED, "retry_failed"))

    def test_empty_major_raises(self):
        with self.assertRaises(ValueError):
            pipeline._extract_structure(FakeClient({"major": "", "minor": [], "entities": []}), "x", "2026-07-06")

    def test_structure_call_passes_major_cache_validator(self):
        client = FakeClient(structure(self.FIXED))
        pipeline._extract_structure(client, "제목. 요약", "2026-07-06")
        validator = client.kwargs[0]["cache_validator"]
        self.assertTrue(validator(json.dumps(structure("x"), ensure_ascii=False)))
        self.assertFalse(validator(json.dumps({"major": " "}, ensure_ascii=False)))


if __name__ == "__main__":
    unittest.main()
