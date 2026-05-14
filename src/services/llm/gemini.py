"""Gemini provider — Google AI API via HTTP."""

import time

from src.config import config
from src.services.llm.base import BaseProvider

BLOCKED_CATEGORIES = [
    "HARM_CATEGORY_HATE_SPEECH",
    "HARM_CATEGORY_DANGEROUS_CONTENT",
    "HARM_CATEGORY_HARASSMENT",
    "HARM_CATEGORY_SEXUALLY_EXPLICIT",
]


class GeminiProvider(BaseProvider):
    @property
    def provider_name(self) -> str:
        return "gemini"

    def _do_generate(self, system_prompt: str, user_prompt: str, call_type: str) -> str:
        if not config.gemini_api_key:
            raise ValueError("GEMINI_API_KEY is not set in .env")

        url = (
            f"https://generativelanguage.googleapis.com/v1beta/"
            f"models/{config.gemini_model}:generateContent"
            f"?key={config.gemini_api_key}"
        )
        payload = {
            "system_instruction": {
                "parts": [{"text": system_prompt}]
            },
            "contents": [
                {
                    "parts": [{"text": user_prompt}]
                }
            ],
            "generationConfig": {
                "temperature": self.temperature,
                "maxOutputTokens": self.max_tokens,
            },
            "safetySettings": [
                {"category": cat, "threshold": "BLOCK_NONE"}
                for cat in BLOCKED_CATEGORIES
            ],
        }

        start = time.monotonic()
        call_id = self._log_request_sent(
            call_type=call_type,
            url=url.split("?")[0],
            request_body=payload,
        )
        response = self._client_instance.post(url, json=payload)
        duration = (time.monotonic() - start) * 1000
        self._check_response(response)
        data = response.json()

        self._log_request_received(
            call_id=call_id,
            call_type=call_type,
            url=url.split("?")[0],
            response_body=data,
            status_code=response.status_code,
            duration_ms=duration,
        )

        candidates = data.get("candidates", [])
        if not candidates:
            block_reason = data.get("promptFeedback", {}).get("blockReason", "")
            if block_reason:
                raise RuntimeError(f"Gemini blocked: {block_reason}")
            raise RuntimeError(f"Gemini returned no candidates: {data}")
        parts = candidates[0].get("content", {}).get("parts", [])
        return "".join(p.get("text", "") for p in parts if not p.get("thought", False)).strip()
