import os
from openai import OpenAI
from dotenv import load_dotenv


class OpenAIClient:
    def __init__(self, model: str = "gpt-4.1-mini", timeout: float | None = None):
        """OpenAI API 호출에 사용할 모델과 timeout을 설정한다."""
        load_dotenv()
        api_key = os.getenv("OPENAI_API_KEY")
        
        if not api_key:
            raise ValueError("OPENAI_API_KEY 환경변수가 설정되지 않았습니다.")

        self.model = model
        self.timeout = timeout if timeout is not None else _load_timeout_seconds()
        self.max_retries = _load_max_retries()
        self._client = OpenAI(api_key=api_key, timeout=self.timeout, max_retries=self.max_retries)

    def request(self, prompt: str, **kwargs) -> str:
        """프롬프트를 OpenAI Chat Completions API로 보내고 응답 본문을 반환한다."""
        response = self._client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            **kwargs,
        )
        
        content = response.choices[0].message.content
        
        if content is None:
            raise ValueError("OpenAI 응답에 content가 없습니다.")
        
        return content


def _load_timeout_seconds() -> float:
    """환경변수에서 OpenAI API timeout 값을 읽는다."""
    raw_value = os.getenv("OPENAI_TIMEOUT_SECONDS", "60")
    try:
        timeout = float(raw_value)
    except ValueError as exc:
        raise ValueError("OPENAI_TIMEOUT_SECONDS는 숫자여야 합니다.") from exc
    if timeout <= 0:
        raise ValueError("OPENAI_TIMEOUT_SECONDS는 0보다 커야 합니다.")
    return timeout


def _load_max_retries() -> int:
    """환경변수에서 OpenAI API 재시도 횟수를 읽는다."""
    raw_value = os.getenv("OPENAI_MAX_RETRIES", "0")
    try:
        max_retries = int(raw_value)
    except ValueError as exc:
        raise ValueError("OPENAI_MAX_RETRIES는 정수여야 합니다.") from exc
    if max_retries < 0:
        raise ValueError("OPENAI_MAX_RETRIES는 0 이상이어야 합니다.")
    return max_retries
