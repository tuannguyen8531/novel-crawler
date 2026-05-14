"""
AI Call Logger — Logs every LLM invocation with full request/response details.

Two log files:
- logs/translation.log      — Summary (1 line per call, compact JSON)
- logs/llm_api.log  — API-level JSON log (actual HTTP request/response)
- logs/error.log    — Error log (structured error messages with stack traces)
"""

import json
import traceback
from datetime import datetime
from pathlib import Path
from uuid import uuid4

LOG_DIR = Path("logs")
LOG_FILE = LOG_DIR / "translation.log"
LOG_API_FILE = LOG_DIR / "llm_api.log"
LOG_ERROR_FILE = LOG_DIR / "error.log"

_verbose = False


def set_verbose(enabled: bool):
    """Enable/disable console output of AI calls."""
    global _verbose
    _verbose = enabled


def _truncate(text: str, max_len: int = 200) -> str:
    """Truncate text for console display."""
    if len(text) <= max_len:
        return text
    return text[:max_len] + f"... ({len(text)} chars total)"


def log_api_request_sent(
    call_type: str,
    provider: str,
    url: str,
    request_body: dict,
    **kwargs,
):
    """Log the HTTP request immediately when sent.

    Returns a call_id to correlate with the response log.
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    call_id = uuid4().hex

    safe_body = _redact_secrets(request_body)

    entry = {
        "type": "request",
        "call_type": call_type,
        "provider": provider,
        "call_id": call_id,
        "url": url,
        "request": safe_body,
        **kwargs,
    }
    with open(LOG_API_FILE, "a", encoding="utf-8") as f:
        f.write(f"{timestamp} {json.dumps(entry, ensure_ascii=False)}\n")

    return call_id


def log_api_request_received(
    call_id: str,
    call_type: str,
    provider: str,
    url: str,
    response_body: dict,
    status_code: int,
    duration_ms: float,
    **kwargs,
):
    """Log the HTTP response after it arrives."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    safe_response = _redact_secrets(response_body)

    entry = {
        "type": "response",
        "call_type": call_type,
        "provider": provider,
        "call_id": call_id,
        "url": url,
        "status_code": status_code,
        "duration_ms": round(duration_ms, 1),
        "response": safe_response,
        **kwargs,
    }
    with open(LOG_API_FILE, "a", encoding="utf-8") as f:
        f.write(f"{timestamp} {json.dumps(entry, ensure_ascii=False)}\n")


def _redact_secrets(data: dict) -> dict:
    """Recursively redact API keys and sensitive fields."""
    redact_keys = {"key", "api_key", "authorization", "token", "secret"}
    if not isinstance(data, dict):
        return data
    result = {}
    for k, v in data.items():
        if k.lower() in redact_keys:
            result[k] = "***REDACTED***"
        elif isinstance(v, dict):
            result[k] = _redact_secrets(v)
        elif isinstance(v, list):
            result[k] = [_redact_secrets(item) if isinstance(item, dict) else item for item in v]
        else:
            result[k] = v
    return result


def log_error(context: str, error: Exception | str, **kwargs):
    """Log an error to error.log with context."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    error_msg = str(error)
    tb = None
    if isinstance(error, Exception):
        tb = "".join(traceback.format_exception(type(error), error, error.__traceback__))

    entry = {
        "context": context,
        "error": error_msg,
        "traceback": tb,
        **kwargs
    }
    
    with open(LOG_ERROR_FILE, "a", encoding="utf-8") as f:
        f.write(f"{timestamp} {json.dumps(entry, ensure_ascii=False)}\n")


def log_ai_call(
    call_type: str,
    system_prompt: str = "",
    user_prompt: str = "",
    response: str = "",
    **kwargs,
):
    """
    Log an AI call to both summary and API log files.

    Args:
        call_type: Type of call (e.g., "translate", "review", "learn_terms", "learn_summary")
        system_prompt: The full system prompt sent to the LLM
        user_prompt: The full user prompt sent to the LLM
        response: The full response from the LLM
        **kwargs: Additional metadata fields to log
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # --- Summary log (compact, 1 line per call) ---
    summary_entry = {
        "type": call_type,
        "system_prompt_len": len(system_prompt),
        "user_prompt_len": len(user_prompt),
        "response_len": len(response),
        **kwargs,
    }
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"{timestamp} {json.dumps(summary_entry, ensure_ascii=False)}\n")

    # --- Console output (verbose mode only) ---
    if _verbose:
        label = call_type.upper()
        if "chunk_index" in kwargs:
            total = kwargs.get("total_chunks", "?")
            label += f" (chunk {kwargs['chunk_index'] + 1}/{total})"
        elif "chapter" in kwargs:
            label += f" (chapter {kwargs['chapter']})"

        print(f"\n{'═' * 60}")
        print(f"[{timestamp}] {label}")
        print(f"{'═' * 60}")
        print(f"--- SYSTEM ({len(system_prompt)} chars) ---")
        print(_truncate(system_prompt))
        print(f"--- USER ({len(user_prompt)} chars) ---")
        print(_truncate(user_prompt))
        print(f"--- RESPONSE ({len(response)} chars) ---")
        print(_truncate(response))
        print(f"{'═' * 60}\n")
