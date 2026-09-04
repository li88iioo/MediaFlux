"""Web Search 与 Web Read 共享的进程内同键请求合并器。"""
from __future__ import annotations

from app.concurrency import KeyedSingleFlight

WEB_REQUEST_SINGLE_FLIGHT_WAIT_SECONDS = 35.0
web_request_singleflight = KeyedSingleFlight(max_entries=256)
