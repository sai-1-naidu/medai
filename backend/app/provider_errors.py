import logging
import re

import httpx

_REDACT_PROVIDER_SECRET = re.compile(
    r"(?i)(gsk_[A-Za-z0-9_-]+|sk[_-][A-Za-z0-9_-]+|Bearer\s+\S+)"
)


def safe_provider_detail(value: object) -> str:
    if not isinstance(value, (str, int, float)):
        return "unknown"
    return _REDACT_PROVIDER_SECRET.sub("[REDACTED]", str(value))[:500]


def log_provider_http_error(
    logger: logging.Logger,
    provider: str,
    response: httpx.Response,
) -> None:
    try:
        body = response.json()
    except ValueError:
        body = {}
    error = body.get("error", {}) if isinstance(body, dict) else {}
    error_code = error.get("code", "unknown") if isinstance(error, dict) else "unknown"
    error_type = error.get("type", "unknown") if isinstance(error, dict) else "unknown"
    error_message = error.get("message", "No provider message.") if isinstance(error, dict) else "No provider message."
    logger.warning(
        "%s request failed: status=%s type=%s code=%s message=%s",
        provider,
        response.status_code,
        safe_provider_detail(error_type),
        safe_provider_detail(error_code),
        safe_provider_detail(error_message),
    )
