from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import httpx


def parse_retry_after(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        delay = max(0.0, float(value))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(parsed, datetime.now(timezone.utc))
    return datetime.now(timezone.utc) + timedelta(seconds=delay)


def request_id(response: httpx.Response) -> str | None:
    return response.headers.get(
        "x-goog-request-id",
        response.headers.get("x-request-id"),
    )
