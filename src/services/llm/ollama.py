"""Ollama provider — local LLM via HTTP API."""

import json
import time
import urllib.error
import urllib.request

from src.config import config
from src.services.llm.base import BaseProvider


class OllamaProvider(BaseProvider):
    @property
    def provider_name(self) -> str:
        return "ollama"

    def _do_generate(self, system_prompt: str, user_prompt: str, call_type: str) -> str:
        url = f"{config.ollama_base_url}/api/chat"
        payload = {
            "model": config.ollama_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "stream": False,
            "options": {
                "temperature": self.temperature,
            },
        }

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"}
        )

        call_id = self._log_request_sent(
            call_type=call_type,
            url=url,
            request_body=payload,
        )

        start = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=300) as response:
                body = response.read().decode("utf-8")
                duration = (time.monotonic() - start) * 1000
                response_data = json.loads(body)

                self._log_request_received(
                    call_id=call_id,
                    call_type=call_type,
                    url=url,
                    response_body=response_data,
                    status_code=response.getcode(),
                    duration_ms=duration,
                )

                return response_data["message"]["content"].strip()
        except urllib.error.HTTPError as e:
            duration = (time.monotonic() - start) * 1000
            try:
                error_body = json.loads(e.read().decode("utf-8"))
            except Exception:
                error_body = {"error": str(e)}

            self._log_request_received(
                call_id=call_id,
                call_type=call_type,
                url=url,
                response_body=error_body,
                status_code=e.code,
                duration_ms=duration,
            )
            raise RuntimeError(f"Ollama API error ({e.code}): {error_body}")
        except urllib.error.URLError as e:
            duration = (time.monotonic() - start) * 1000
            self._log_request_received(
                call_id=call_id,
                call_type=call_type,
                url=url,
                response_body={"error": str(e)},
                status_code=0,
                duration_ms=duration,
            )
            raise RuntimeError(f"Ollama API error: {e}")
