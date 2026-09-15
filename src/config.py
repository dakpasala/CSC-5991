"""Shared project paths and reviewed-session validation."""

import argparse
import json
import math
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
CATEGORIES = (
    "web_browsing",
    "social_media_browsing",
    "video_streaming",
    "audio_streaming",
    "file_download",
    "video_conferencing",
)
BROWSING = {"web_browsing", "social_media_browsing"}


def positive(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("Expected a finite positive number")
    return number


def validate_url(url):
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ValueError("Expected an HTTP(S) URL without embedded credentials")


def available(item):
    if item.get("enabled", True) is not True:
        return False
    if item["activity"] == "video_conferencing":
        return bool(
            item.get("controlled_test") is True
            and item.get("participants_ready") is True
            and item.get("joined_selector")
            and os.environ.get(item.get("meeting_url_env", ""))
        )
    return True


def load_plan(path, category):
    items = json.loads(Path(path).read_text())
    if not isinstance(items, list) or not items:
        raise ValueError("Plan must be a nonempty JSON list")
    for item in items:
        if not isinstance(item, dict) or item.get("activity") != category:
            raise ValueError(f"This command only accepts {category} sessions")
        if not re.fullmatch(r"[a-z0-9.-]+", item.get("domain", "")):
            raise ValueError("Invalid domain")
        if category != "video_conferencing":
            validate_url(item.get("url", ""))
        if category == "social_media_browsing" and not item.get("content_selector"):
            raise ValueError("Social browsing requires a post/feed content selector")
        if category == "file_download":
            validate_url(item.get("download_url", ""))
            if urlsplit(item["url"]).netloc != urlsplit(item["download_url"]).netloc:
                raise ValueError("Downloads must share the landing page's origin")
        for link in item.get("links", []):
            validate_url(link)
            if urlsplit(link).netloc != urlsplit(item["url"]).netloc:
                raise ValueError("Browsing links must stay on the configured host")
        for step in item.get("steps", []):
            if step.get("type") not in {"click", "wait", "fill"}:
                raise ValueError("Steps support click, wait, or fill")
            if not step.get("selector"):
                raise ValueError("Each step requires a CSS selector")
            if step["type"] == "fill" and not step.get("value_env"):
                raise ValueError("Fill values must come from environment variables")
    return items
