"""
agents/web_agent/tools.py — Web search and fetch tool implementations.

Supports multiple search backends (SearXNG, Brave) and webpage content
extraction.  HTML pages are converted to Markdown via MarkItDown so link
hrefs, tables, and images survive the fetch → LLM pipeline.
"""

from __future__ import annotations

import asyncio
import html
import io
import json
import logging
import re
from functools import partial
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

logger = logging.getLogger("web_agent.tools")

USER_AGENT = "Mozilla/5.0 (compatible; WebAgent/1.0)"


# ---------------------------------------------------------------------------
# MarkItDown singleton (lazy-loaded — avoids paying the import cost until
# the first HTML fetch; MarkItDown is already a project dependency via
# md_converter, so the module is usually resident in the router process).
# ---------------------------------------------------------------------------

_markitdown_converter: Any = None


def _get_markitdown() -> Any:
    """Return a shared MarkItDown instance, constructed on first use.

    OCR is intentionally not enabled here — web_fetch targets HTML pages,
    not scanned documents, so the OCR plugin would only add startup cost.
    """
    global _markitdown_converter
    if _markitdown_converter is None:
        from markitdown import MarkItDown
        _markitdown_converter = MarkItDown()
    return _markitdown_converter


def _html_to_markdown(raw_html: str) -> str:
    """Convert an HTML string to Markdown via MarkItDown.

    Runs in a thread because MarkItDown.convert_stream is synchronous.
    On any failure, falls back to the regex-based ``_html_to_text``
    converter so a single bad page can't break web_fetch.
    """
    try:
        converter = _get_markitdown()
        buf = io.BytesIO(raw_html.encode("utf-8"))
        result = converter.convert_stream(buf, file_extension=".html")
        return _normalize(result.text_content or "")
    except Exception as exc:
        logger.warning("MarkItDown HTML conversion failed, falling back to regex: %s", exc)
        return _html_to_text(raw_html)


# ---------------------------------------------------------------------------
# HTML helpers
# ---------------------------------------------------------------------------


def _strip_tags(text: str) -> str:
    """Remove HTML tags and decode entities."""
    text = re.sub(r"<script[\s\S]*?</script>", "", text, flags=re.I)
    text = re.sub(r"<style[\s\S]*?</style>", "", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def _strip_non_printable(text: str) -> str:
    """Remove control characters and non-printable Unicode that can break UIs."""
    # Keep newline, tab, and standard printable characters
    return re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\ufeff\ufffe\uffff]', '', text)


def _normalize(text: str) -> str:
    """Collapse excessive whitespace and strip non-printable characters."""
    text = _strip_non_printable(text)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _html_to_text(raw_html: str) -> str:
    """Convert HTML to readable plain text."""
    # Preserve some structure
    text = re.sub(r"<h([1-6])[^>]*>([\s\S]*?)</h\1>",
                  lambda m: f"\n{'#' * int(m[1])} {_strip_tags(m[2])}\n", raw_html, flags=re.I)
    text = re.sub(r"<li[^>]*>([\s\S]*?)</li>", lambda m: f"\n- {_strip_tags(m[1])}", text, flags=re.I)
    text = re.sub(r"</(p|div|section|article)>", "\n\n", text, flags=re.I)
    text = re.sub(r"<(br|hr)\s*/?>", "\n", text, flags=re.I)
    return _normalize(_strip_tags(text))


def _format_results(
    query: str,
    items: list[dict[str, Any]],
    n: int,
    content_len_limit: int = 0,
) -> str:
    """Format search results into plain text."""
    if not items:
        return f"No results for: {query}"
    lines = [f"Results for: {query}\n"]
    for i, item in enumerate(items[:n], 1):
        title = _normalize(_strip_tags(item.get("title", "")))
        snippet = _normalize(_strip_tags(item.get("content", "")))
        if content_len_limit and len(snippet) > content_len_limit:
            snippet = snippet[:content_len_limit] + "..."
        url = item.get("url", "")
        published = item.get("publishedDate")
        lines.append(f"{i}. {title}\n   {url}")
        if published:
            lines.append(f"   Published: {published}")
        if snippet:
            lines.append(f"   {snippet}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Search backends
# ---------------------------------------------------------------------------


async def search_searxng(
    query: str,
    base_url: str,
    count: int = 5,
    timeout: float = 10.0,
    time_range: Optional[str] = None,
    language: Optional[str] = None,
    content_len_limit: int = 0,
) -> str:
    """Search via self-hosted SearXNG instance."""
    if not base_url:
        return "Error: SEARXNG_BASE_URL not configured"
    endpoint = f"{base_url.rstrip('/')}/search"
    params: dict[str, Any] = {"q": query, "format": "json"}
    if time_range:
        params["time_range"] = time_range
    if language:
        params["language"] = language
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(
                endpoint,
                params=params,
                headers={"User-Agent": USER_AGENT},
            )
            r.raise_for_status()
        results = r.json().get("results", [])
        return _format_results(query, results, count, content_len_limit)
    except Exception as e:
        return f"Error: SearXNG search failed: {e}"


async def search_brave(
    query: str, api_key: str, count: int = 5, timeout: float = 10.0,
) -> str:
    """Search via Brave Search API."""
    if not api_key:
        return "Error: BRAVE_API_KEY not configured"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(
                "https://api.search.brave.com/res/v1/web/search",
                params={"q": query, "count": count},
                headers={
                    "Accept": "application/json",
                    "X-Subscription-Token": api_key,
                },
            )
            r.raise_for_status()
        items = [
            {
                "title": x.get("title", ""),
                "url": x.get("url", ""),
                "content": x.get("description", ""),
            }
            for x in r.json().get("web", {}).get("results", [])
        ]
        return _format_results(query, items, count)
    except Exception as e:
        return f"Error: Brave search failed: {e}"


async def web_search(
    query: str,
    provider: str = "searxng",
    searxng_base_url: str = "",
    brave_api_key: str = "",
    count: int = 5,
    timeout: float = 10.0,
    time_range: Optional[str] = None,
    language: Optional[str] = None,
    content_len_limit: int = 0,
) -> str:
    """Dispatch to the configured search backend."""
    provider = provider.strip().lower()
    if provider == "searxng":
        return await search_searxng(
            query, searxng_base_url, count, timeout,
            time_range=time_range, language=language,
            content_len_limit=content_len_limit,
        )
    elif provider == "brave":
        return await search_brave(query, brave_api_key, count, timeout)
    else:
        return f"Error: unknown search provider '{provider}' (supported: searxng, brave)"


# ---------------------------------------------------------------------------
# Web fetch
# ---------------------------------------------------------------------------


def _validate_url(url: str) -> tuple[bool, str]:
    """Basic URL validation."""
    try:
        p = urlparse(url)
        if p.scheme not in ("http", "https"):
            return False, f"Only http/https allowed, got '{p.scheme or 'none'}'"
        if not p.netloc:
            return False, "Missing domain"
        return True, ""
    except Exception as e:
        return False, str(e)


async def web_fetch(
    url: str,
    max_chars: int = 20000,
    timeout: float = 15.0,
) -> str:
    """Fetch a URL and extract readable text content."""
    ok, err = _validate_url(url)
    if not ok:
        return json.dumps({"error": f"Invalid URL: {err}", "url": url})

    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            max_redirects=5,
            timeout=timeout,
        ) as client:
            r = await client.get(url, headers={"User-Agent": USER_AGENT})
            r.raise_for_status()

        ctype = r.headers.get("content-type", "")

        if "application/json" in ctype:
            text = json.dumps(r.json(), indent=2, ensure_ascii=False)
        elif "text/html" in ctype or r.text[:256].lower().startswith(("<!doctype", "<html")):
            # MarkItDown is synchronous and can take tens of ms on large pages;
            # offload to the default executor to keep the event loop responsive.
            loop = asyncio.get_running_loop()
            text = await loop.run_in_executor(None, partial(_html_to_markdown, r.text))
        else:
            text = _normalize(r.text)

        truncated = len(text) > max_chars
        if truncated:
            text = text[:max_chars] + "\n\n[Content truncated]"

        return json.dumps({
            "url": url,
            "final_url": str(r.url),
            "status": r.status_code,
            "truncated": truncated,
            "length": len(text),
            "text": text,
        }, ensure_ascii=False)

    except Exception as e:
        return json.dumps({"error": str(e), "url": url})


# ---------------------------------------------------------------------------
# Weather (Open-Meteo — free, no API key)
# ---------------------------------------------------------------------------

_WMO_WEATHER_CODES: dict[int, str] = {
    0: "Clear sky",
    1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Depositing rime fog",
    51: "Drizzle: Light", 53: "Drizzle: Moderate", 55: "Drizzle: Dense",
    56: "Freezing drizzle: Light", 57: "Freezing drizzle: Dense",
    61: "Rain: Slight", 63: "Rain: Moderate", 65: "Rain: Heavy",
    66: "Freezing rain: Light", 67: "Freezing rain: Heavy",
    71: "Snow: Slight", 73: "Snow: Moderate", 75: "Snow: Heavy",
    77: "Snow grains",
    80: "Rain showers: Slight", 81: "Rain showers: Moderate", 82: "Rain showers: Violent",
    85: "Snow showers: Slight", 86: "Snow showers: Heavy",
    95: "Thunderstorm", 96: "Thunderstorm with slight hail", 99: "Thunderstorm with heavy hail",
}


def _translate_weather_codes(data: Any) -> Any:
    if isinstance(data, dict):
        for key, value in data.items():
            if key in ("weather_code", "weathercode"):
                if isinstance(value, list):
                    data[key] = [_WMO_WEATHER_CODES.get(int(c), f"Unknown ({c})") for c in value]
                elif isinstance(value, (int, float)):
                    data[key] = _WMO_WEATHER_CODES.get(int(value), f"Unknown ({value})")
            else:
                _translate_weather_codes(value)
    elif isinstance(data, list):
        for item in data:
            _translate_weather_codes(item)
    return data


async def _geocode(location: str, timeout: float = 10.0) -> tuple[float, float]:
    headers = {"User-Agent": "Backplaned-WebAgent/1.0"}
    url = "https://nominatim.openstreetmap.org/search"
    params = {"q": location, "format": "json", "limit": "1"}
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.get(url, params=params, headers=headers)
        r.raise_for_status()
    data = r.json()
    if not data:
        raise ValueError(f"Could not resolve location: {location}")
    return float(data[0]["lat"]), float(data[0]["lon"])


async def weather(
    location: str,
    mode: str = "now",
    imperial: bool = False,
    timeout: float = 10.0,
) -> str:
    """Fetch weather from Open-Meteo (free, no API key)."""
    try:
        lat, lon = await _geocode(location, timeout)
    except Exception as e:
        return json.dumps({"error": f"Geocoding failed: {e}"})

    units = "&wind_speed_unit=mph&temperature_unit=fahrenheit&precipitation_unit=inch" if imperial else ""
    base = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&timezone=auto{units}"

    daily_params = "weather_code,temperature_2m_max,temperature_2m_min,apparent_temperature_max,apparent_temperature_min,precipitation_probability_max,precipitation_sum,wind_speed_10m_max,sunrise,sunset"
    hourly_params = "temperature_2m,relative_humidity_2m,precipitation_probability,precipitation,weather_code,wind_speed_10m"

    mode_map: dict[str, str] = {
        "now":      f"{base}&current=temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,weather_code,wind_speed_10m,wind_direction_10m",
        "today":    f"{base}&daily={daily_params}&forecast_days=1",
        "tomorrow": f"{base}&daily={daily_params}&forecast_days=2",
        "8hourly":  f"{base}&hourly={hourly_params}&forecast_hours=8",
        "24hourly": f"{base}&hourly={hourly_params}&forecast_hours=24",
        "7days":    f"{base}&daily={daily_params}&forecast_days=7",
        "16days":   f"{base}&daily={daily_params}&forecast_days=16",
    }

    url = mode_map.get(mode)
    if not url:
        return json.dumps({"error": f"Unknown mode: {mode}. Use: now, today, tomorrow, 8hourly, 24hourly, 7days, 16days"})

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(url)
            r.raise_for_status()
        data = _translate_weather_codes(r.json())
        return json.dumps(data, indent=2, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": f"Open-Meteo request failed: {e}"})


# ---------------------------------------------------------------------------
# Tool definitions (OpenAI function-tool format)
# ---------------------------------------------------------------------------

WEB_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Search the web for information. Returns titles, URLs, and snippets. "
                "Use specific, targeted queries for best results."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query",
                    },
                    "count": {
                        "type": "integer",
                        "description": "Number of results (1-10, default 5)",
                        "minimum": 1,
                        "maximum": 10,
                    },
                    "time_range": {
                        "type": "string",
                        "enum": ["day", "week", "month", "year"],
                        "description": "Filter results by time range (SearXNG only)",
                    },
                    "language": {
                        "type": "string",
                        "description": "Language code for results, e.g. 'en', 'ko' (SearXNG only)",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": (
                "Fetch a webpage and return its content as Markdown — links, "
                "tables, and images are preserved (truncated to ~12k chars). "
                "Use sparingly — only fetch the most relevant URLs."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The URL to fetch",
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "weather",
            "description": (
                "Get weather data from Open-Meteo (free, no API key). "
                "Always use this tool instead of web_search for weather queries.\n"
                "Examples:\n"
                '  weather(location="Seoul", mode="now") → current conditions\n'
                '  weather(location="Riverside, CA", mode="today") → today\'s summary\n'
                '  weather(location="Riverside, CA", mode="tomorrow") → tomorrow\'s summary\n'
                '  weather(location="Tokyo", mode="8hourly") → next 8 hours\n'
                '  weather(location="Tokyo", mode="24hourly") → next 24 hours\n'
                '  weather(location="London", mode="7days") → 7-day forecast\n'
                '  weather(location="London", mode="16days") → 16-day forecast'
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": "City or place name. Examples: 'Seoul', 'Riverside, CA', 'Tokyo, Japan'",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["now", "today", "tomorrow", "8hourly", "24hourly", "7days", "16days"],
                        "description": "now: current conditions. today: today's summary. tomorrow: tomorrow's summary. 8hourly: next 8 hours. 24hourly: next 24 hours. 7days: 7-day forecast. 16days: 16-day forecast. Default: now",
                    },
                    "imperial": {
                        "type": "boolean",
                        "description": "Set true for °F/mph/inches, false for °C/km·h/mm. Default: false (metric)",
                    },
                },
                "required": ["location"],
            },
        },
    },
]
