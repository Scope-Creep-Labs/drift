import json
from typing import Any


def sse(event: str, data: Any) -> bytes:
    """Format a single Server-Sent Event."""
    payload = json.dumps(data, default=str, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n".encode("utf-8")
