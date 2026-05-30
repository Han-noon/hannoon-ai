import os
from openai import OpenAI
from dotenv import load_dotenv


class OpenAIClient:
    def __init__(self, model: str = "gpt-4.1-mini"):
        load_dotenv()
        api_key = os.getenv("OPENAI_API_KEY")
        
        if not api_key:
            raise ValueError("OPENAI_API_KEY 환경변수가 설정되지 않았습니다.")

        self.model = model
        self._client = OpenAI(api_key=api_key)

    def request(self, prompt: str, **kwargs) -> str:
        response = self._client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            **kwargs,
        )
        
        content = response.choices[0].message.content
        
        if content is None:
            raise ValueError("OpenAI 응답에 content가 없습니다.")
        
        return content
