import os
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv(override=True)  # 시스템 환경변수보다 .env 파일 값을 우선 적용


class OpenAIClient:
    def __init__(self, model: str = "gpt-4.1-mini"):
        self.api_key = os.getenv("OPENAI_API_KEY")
        self.model = model
        self._client = OpenAI(api_key=self.api_key)

    def request(self, prompt: str, **kwargs) -> str:
        response = self._client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            **kwargs,
        )
        return response.choices[0].message.content
