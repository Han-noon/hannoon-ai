from __future__ import annotations

from pathlib import Path
import os
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
os.environ.setdefault("USE_TF", "0")

import embedding


# 실측 v12 major(#3399) + #4397 일부 — 128토큰을 확실히 넘는 길이
LONG_MAJOR = (
    "2026년 7월 7일, 아야톨라 세예드 알리 하메네이 전 이란 최고지도자의 시신이 이란의 종교도시 곰에 도착했다. "
    "하메네이는 미군과 이스라엘의 공습으로 사망했으며, 장례 절차는 4일 테헤란에서 시작되었다. "
    "곰에서는 7일 시아파 고위 성직자들이 참석한 가운데 장례 예배가 진행될 예정이다. "
    "이후 시신은 8일 이라크를 거쳐 9일 마슈하드에 안장될 계획이다. "
    "이 장례 절차는 이란 내에서 큰 정치적 및 종교적 영향을 미칠 것으로 예상된다. "
    "유족들은 약 1300km 떨어진 테헤란까지 수일간 이동하여 장례식에 참여했으며, 미국은 해당 공습에 대한 책임을 인정하지 않고 있다."
)


class FitSentencesTest(unittest.TestCase):
    def setUp(self):
        self.model = embedding._get_model()

    def tokens(self, text):
        return len(self.model.tokenizer(text, truncation=False)["input_ids"])

    def test_long_text_is_cut_at_sentence_boundary_within_limit(self):
        self.assertGreater(self.tokens(LONG_MAJOR), self.model.max_seq_length)
        fitted = embedding.fit_sentences_to_max_tokens(LONG_MAJOR)
        self.assertLessEqual(self.tokens(fitted), self.model.max_seq_length)
        self.assertTrue(LONG_MAJOR.startswith(fitted))
        self.assertTrue(fitted.endswith("."))
        self.assertIn("곰에 도착했다.", fitted)

    def test_short_text_is_unchanged(self):
        text = "2026년 7월 7일 나토 정상회의가 개막할 예정이다. 회원국 정상들이 참석한다."
        self.assertEqual(embedding.fit_sentences_to_max_tokens(text), text)

    def test_first_sentence_is_always_kept(self):
        first = " ".join(["하메네이 장례식"] * 80) + "."
        self.assertEqual(embedding.fit_sentences_to_max_tokens(first + " 둘째 문장."), first)


if __name__ == "__main__":
    unittest.main()
