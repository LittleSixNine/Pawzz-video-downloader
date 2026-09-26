"""Offline protocol helpers for the app's timeline recordings (APK 2.2.0)."""

from __future__ import annotations

from datetime import date
import time
from urllib.parse import urlsplit


def _head(command: str, user_id: str, request_id: str | None = None) -> dict:
    if not user_id:
        raise ValueError("user_id is required")
    return {
        "reply": True,
        "req_id": request_id or f"app-{time.time_ns() // 1_000_000}",
        "origin": "app",
        "user_id": user_id,
        "cmd": command,
    }


def day_request(user_id: str, request_id: str | None = None) -> dict:
    return {"head": _head("list_record_day", user_id, request_id), "body": {}}


def detail_request(user_id: str, day: date, page: int = 1, request_id: str | None = None) -> dict:
    if page < 1:
        raise ValueError("page must start at 1")
    day_text = day.strftime("%Y/%m/%d")
    return {
        "head": _head("list_record_detail", user_id, request_id),
        "body": {
            "s_sec": f"{day_text} 00:00:00",
            "e_sec": f"{day_text} 23:59:59",
            "curPage": page,
            "pageSize": 20,
        },
    }


def download_request(user_id: str, start: str, end: str, request_id: str | None = None) -> dict:
    """Ask the device to upload one recording and return an MP4 URL later."""
    if not start or not end:
        raise ValueError("start and end are required")
    return {
        "head": _head("req_download", user_id, request_id),
        "body": {
            "type": "playback",
            "s_sec": start,
            "e_sec": end,
            "video_id": 2,
            "img_url": "",
        },
    }


def parse_download_done(message: dict) -> str:
    """Read the URL from the device's req_download_done notification."""
    if not isinstance(message, dict) or not isinstance(message.get("head"), dict):
        raise ValueError("MQTT response must have a head")
    if message["head"].get("cmd") != "req_download_done":
        raise ValueError("not a req_download_done response")
    body = message.get("body")
    if not isinstance(body, dict) or body.get("status") is not True:
        raise ValueError("recording upload did not succeed")
    url = body.get("mp4url")
    if not isinstance(url, str) or urlsplit(url).scheme not in ("http", "https") or not urlsplit(url).netloc:
        raise ValueError("missing or invalid MP4 URL")
    return url


def parse_day_response(message: dict) -> list[str]:
    if not isinstance(message, dict) or not isinstance(message.get("head"), dict):
        raise ValueError("MQTT response must have a head")
    if message["head"].get("cmd") != "list_record_day" or message.get("code", 0) != 0:
        raise ValueError("list_record_day failed or command did not match")
    body = message.get("body")
    days = body.get("date", []) if isinstance(body, dict) else None
    if not isinstance(days, list) or any(not isinstance(day, str) for day in days):
        raise ValueError("list_record_day date must be a list of strings")
    return [date.fromisoformat(day.replace("/", "-")).isoformat() for day in days]


def parse_detail_response(message: dict, expected_page: int) -> tuple[list[dict], int]:
    """Return one validated page and its total page count; retain source values."""
    if not isinstance(message, dict):
        raise ValueError("MQTT response must be an object")
    head = message.get("head")
    if not isinstance(head, dict) or head.get("cmd") != "list_record_detail":
        raise ValueError("not a list_record_detail response")
    if message.get("code", 0) != 0:
        raise ValueError("list_record_detail failed")
    body = message.get("body")
    if not isinstance(body, dict):
        raise ValueError("list_record_detail body must be an object")
    page = body.get("curPage", 1)
    total = body.get("totalPage", 1)
    if expected_page < 1 or type(page) is not int or type(total) is not int or page != expected_page or total < page:
        raise ValueError("invalid or unexpected page numbers")
    records = body.get("video_list", [])
    if not isinstance(records, list):
        raise ValueError("video_list must be a list")
    if not records and page < total:
        raise ValueError("empty page before the final page")
    for record in records:
        if not isinstance(record, dict) or not all(key in record for key in ("video_id", "s_sec", "e_sec")):
            raise ValueError("invalid timeline record")
    return records, total
