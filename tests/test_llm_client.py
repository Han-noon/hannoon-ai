from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

import httpx
import openai

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from openai_client import client as C


def ok(content):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def rate_limited(retry_after=None):
    headers = {"retry-after": str(retry_after)} if retry_after is not None else {}
    resp = httpx.Response(429, headers=headers, request=httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions"))
    return openai.RateLimitError("Provider returned error", response=resp, body=None)


class ClientTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        env = {"LLM_API_KEY": "test", "LLM_CACHE_DIR": self.tmp.name, "LLM_MAX_RPM": "0", "LLM_TRANSIENT_RETRIES": "3"}
        self.env = mock.patch.dict(os.environ, env)
        self.env.start()
        self.dotenv = mock.patch.object(C, "load_dotenv", lambda **kw: None)  # 실제 .env가 테스트 값을 덮지 않게
        self.dotenv.start()
        self.sleep = mock.patch.object(C.time, "sleep")
        self.slept = self.sleep.start()
        self.llm = C.LLMClient(model="m", provider_only=[])
        self.create = mock.MagicMock()
        self.llm._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=self.create)))

    def tearDown(self):
        self.sleep.stop(); self.dotenv.stop(); self.env.stop(); self.tmp.cleanup()

    def ask(self, **kw):
        return self.llm.request("p", response_format={"type": "json_object"}, temperature=0, **kw)


class TransientRetryTest(ClientTestBase):
    def test_rate_limit_is_retried_in_place_with_backoff(self):
        self.create.side_effect = [rate_limited(), rate_limited(), ok('{"a": 1}')]
        self.assertEqual(self.ask(), '{"a": 1}')
        self.assertEqual(self.create.call_count, 3)
        self.assertEqual([c.args[0] for c in self.slept.call_args_list], [10.0, 20.0])

    def test_longer_retry_after_header_is_honoured(self):
        self.create.side_effect = [rate_limited(retry_after=30), ok('{"a": 1}')]
        self.ask()
        self.assertEqual(self.slept.call_args_list[0].args[0], 30.0)

    def test_small_retry_after_does_not_shorten_backoff(self):
        self.create.side_effect = [rate_limited(), rate_limited(retry_after=1), ok('{"a": 1}')]
        self.ask()
        self.assertEqual([c.args[0] for c in self.slept.call_args_list], [10.0, 20.0])

    def test_server_and_connection_errors_are_retried(self):
        req = httpx.Request("POST", "https://x")
        self.create.side_effect = [
            openai.InternalServerError("upstream", response=httpx.Response(502, request=req), body=None),
            openai.APIConnectionError(request=req),
            ok('{"a": 1}'),
        ]
        self.assertEqual(self.ask(), '{"a": 1}')
        self.assertEqual(self.create.call_count, 3)

    def test_sdk_internal_retries_are_disabled(self):
        self.assertEqual(C.LLMClient(model="m", provider_only=[])._client.max_retries, 0)

    def test_gives_up_after_configured_retries(self):
        self.create.side_effect = rate_limited()
        with self.assertRaises(openai.RateLimitError):
            self.ask()
        self.assertEqual(self.create.call_count, 4)  # 최초 1 + 재시도 3

    def test_non_transient_error_is_not_retried(self):
        resp = httpx.Response(400, request=httpx.Request("POST", "https://x"))
        self.create.side_effect = openai.BadRequestError("bad", response=resp, body=None)
        with self.assertRaises(openai.BadRequestError):
            self.ask()
        self.assertEqual(self.create.call_count, 1)


class RateLimiterTest(unittest.TestCase):
    def test_calls_are_spaced_by_60_over_rpm(self):
        clock = {"t": 100.0}
        waits = []
        with mock.patch.object(C.time, "monotonic", lambda: clock["t"]), \
             mock.patch.object(C.time, "sleep", lambda s: (waits.append(s), clock.__setitem__("t", clock["t"] + s))):
            limiter = C._RateLimiter()
            for _ in range(3):
                limiter.wait(10)  # 10 rpm → 6초 간격
        self.assertEqual(waits, [6.0, 6.0])

    def test_zero_rpm_disables_limit(self):
        with mock.patch.object(C.time, "sleep") as sleep:
            limiter = C._RateLimiter()
            for _ in range(5):
                limiter.wait(0)
        sleep.assert_not_called()


class CacheValidationTest(ClientTestBase):
    def test_broken_json_is_not_cached(self):
        self.create.side_effect = [ok("not json"), ok('{"major": "x"}')]
        self.assertEqual(self.ask(), "not json")
        self.assertEqual(self.ask(), '{"major": "x"}')  # 캐시에 없으니 다시 호출
        self.assertEqual(self.create.call_count, 2)
        self.assertEqual(self.ask(), '{"major": "x"}')  # 이번엔 캐시 적중
        self.assertEqual(self.create.call_count, 2)

    def test_validator_failure_is_not_cached(self):
        has_major = lambda c: bool(json.loads(c).get("major"))
        self.create.side_effect = [ok('{"major": ""}'), ok('{"major": "y"}')]
        self.ask(cache_validator=has_major)
        self.assertEqual(self.ask(cache_validator=has_major), '{"major": "y"}')
        self.assertEqual(self.create.call_count, 2)

    def test_previously_cached_broken_response_is_discarded(self):
        path = self.llm._cache_path("p", {"response_format": {"type": "json_object"}, "temperature": 0})
        with open(path, "w", encoding="utf-8") as fp:
            json.dump({"model": "m", "content": "{broken"}, fp)
        self.create.side_effect = [ok('{"major": "z"}')]
        self.assertEqual(self.ask(), '{"major": "z"}')
        with open(path, encoding="utf-8") as fp:
            self.assertEqual(json.load(fp)["content"], '{"major": "z"}')

    def test_truncated_cache_file_is_discarded(self):
        path = self.llm._cache_path("p", {"response_format": {"type": "json_object"}, "temperature": 0})
        with open(path, "w", encoding="utf-8") as fp:
            fp.write('{"model": "m", "content": "{\\"ma')  # 쓰다 끊긴 파일
        self.create.side_effect = [ok('{"major": "w"}')]
        self.assertEqual(self.ask(), '{"major": "w"}')

    def test_validator_exception_means_not_cached(self):
        def boom(_):
            raise KeyError("x")
        self.create.side_effect = [ok('{"major": "v"}'), ok('{"major": "v"}')]
        self.ask(cache_validator=boom)
        self.ask(cache_validator=boom)
        self.assertEqual(self.create.call_count, 2)

    def test_validator_does_not_change_cache_key(self):
        # 기존 캐시(검증기 도입 전 저장분)를 그대로 재사용할 수 있어야 한다
        self.create.side_effect = [ok('{"major": "k"}')]
        self.ask()
        self.ask(cache_validator=lambda c: True)
        self.assertEqual(self.create.call_count, 1)


if __name__ == "__main__":
    unittest.main()
