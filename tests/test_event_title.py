from __future__ import annotations

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from event_summary import generate_event_title


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


class EventTitleTests(unittest.TestCase):
    def test_generate_event_title_normalizes_and_falls_back_to_headline(self):
        title = generate_event_title(
            FakeLLM({"title": "서울 아파트값 30주 연속 상승."}),
            article_title="“또 올랐다”…서울 아파트값",
            major="m",
            published_at="2026-07-07",
        )
        self.assertEqual(title, "서울 아파트값 30주 연속 상승")

        for client in (FakeLLM(RuntimeError("boom")), FakeLLM({"title": " "})):
            title = generate_event_title(
                client,
                article_title="  “또 올랐다”…서울   아파트값 ",
                major="m",
                published_at="2026-07-07",
            )
            self.assertEqual(title, "“또 올랐다”…서울 아파트값")

    def test_generate_event_title_never_cuts_long_title(self):
        long_title = "이재명 대통령 NATO 정상회의 참석 후 몽골 국빈 방문 및 한몽 공동선언 발표 일정 확정."
        client = FakeLLM({"title": long_title})
        title = generate_event_title(client, article_title="h", major="m", published_at="2026-07-07")
        self.assertEqual(title, long_title.rstrip("."))


if __name__ == "__main__":
    unittest.main()
