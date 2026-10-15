from __future__ import annotations

import re
import time
from collections.abc import Iterator
from typing import Any

import requests

from config import OPENALEX_API_KEY, OPENALEX_BASE_URL, OPENALEX_EMAIL

OPENALEX_TIMEOUT = 20
MAX_RETRY_WAIT = 60.0
# Server errors and dropped connections: OpenAlex restarts can take minutes, so wait them out
# (about 12 minutes in all) before calling the service unavailable.
OUTAGE_WAITS = (10, 30, 60, 120, 240, 300)


class OpenAlexUnavailable(requests.RequestException):
    """OpenAlex refused or kept failing (rate limit, daily budget, outage).

    Callers must not treat this as "no results": stop and retry next run.
    """


def normalize_openalex_id(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    return value.rstrip("/").split("/")[-1]


def _tokens(value: str) -> set[str]:
    stop = {"university", "college", "institute", "hospital", "school", "national", "research"}
    return {
        token
        for token in re.findall(r"[a-z0-9]+", (value or "").casefold())
        if len(token) >= 3 and token not in stop
    }


class OpenAlexClient:
    def __init__(self) -> None:
        self.session = requests.Session()
        user_agent = "AboutBio-KoreaBioMap/0.4"
        if OPENALEX_EMAIL:
            user_agent += f" (mailto:{OPENALEX_EMAIL})"
        self.session.headers.update({"User-Agent": user_agent})
        # Polite pool (mailto) / API key allow ~10 req/s; stay under it.
        self.min_interval_seconds = 0.12 if (OPENALEX_EMAIL or OPENALEX_API_KEY) else 0.35
        self.max_retries = 4
        self._last_request_at = 0.0
        self._reported = False
        self.request_counts: dict[str, int] = {}

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.min_interval_seconds:
            time.sleep(self.min_interval_seconds - elapsed)

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        params = dict(params or {})
        if OPENALEX_EMAIL:
            params["mailto"] = OPENALEX_EMAIL
        if OPENALEX_API_KEY:
            params["api_key"] = OPENALEX_API_KEY

        url = f"{OPENALEX_BASE_URL}/{path.lstrip('/')}"
        last_error = ""
        kind = f"{path.split('/')[0]}:{'search' if 'search' in params else 'get' if '/' in path else 'list'}"
        self.request_counts[kind] = self.request_counts.get(kind, 0) + 1

        attempt, outages = 0, 0
        while attempt < self.max_retries:
            attempt += 1
            self._throttle()
            try:
                response = self.session.get(url, params=params, timeout=OPENALEX_TIMEOUT)
            except requests.RequestException as exc:
                self._last_request_at = time.monotonic()
                last_error = f"{type(exc).__name__}"
                if outages < len(OUTAGE_WAITS):
                    time.sleep(OUTAGE_WAITS[outages])
                    outages += 1
                    attempt -= 1        # an outage does not use up the ordinary retries
                continue
            self._last_request_at = time.monotonic()

            if response.status_code == 200:
                try:
                    return response.json()
                except ValueError:
                    last_error = "invalid JSON"
                    continue

            body = (response.text or "")[:300].replace("\n", " ")
            last_error = f"HTTP {response.status_code}: {body}"
            if response.status_code == 429:
                # A per-second limit clears quickly; a daily budget does not.
                if "daily" in body.casefold() or "budget" in body.casefold() or "credit" in body.casefold():
                    break
                try:
                    wait = float(response.headers.get("Retry-After", ""))
                except (TypeError, ValueError):
                    wait = 5.0 * (attempt + 1)
                time.sleep(min(max(wait, 1.0), MAX_RETRY_WAIT))
                continue
            if 500 <= response.status_code < 600:
                if outages < len(OUTAGE_WAITS):
                    print(f"[openalex] HTTP {response.status_code}; waiting {OUTAGE_WAITS[outages]}s", flush=True)
                    time.sleep(OUTAGE_WAITS[outages])
                    outages += 1
                    attempt -= 1
                continue
            if response.status_code in (401, 403):
                break
            # Other 4xx: a bad request, not an outage.
            response.raise_for_status()

        message = f"OpenAlex unavailable ({last_error or 'no response'})"
        if not self._reported:
            self._reported = True
            print(f"[openalex] {message}", flush=True)
            print("[openalex] If this is a rate limit or daily budget, add an OPENALEX_API_KEY secret "
                  "(free key from openalex.org) and re-run.", flush=True)
        raise OpenAlexUnavailable(message)

    def get_author(self, author_id: str) -> dict[str, Any] | None:
        author_id = normalize_openalex_id(author_id)
        if not author_id:
            return None
        try:
            return self._get(f"authors/{author_id}")
        except requests.RequestException:
            return None

    def search_author(self, name: str, institution: str = "") -> dict[str, Any] | None:
        try:
            data = self._get("authors", {"search": name, "per-page": 10})
        except requests.RequestException:
            return None

        results = data.get("results", [])
        if not results or not institution:
            return None

        target_tokens = _tokens(institution)
        best = None
        best_overlap = 0
        for author in results:
            institutions = author.get("last_known_institutions") or []
            haystack = " ".join(
                item.get("display_name", "")
                for item in institutions
                if isinstance(item, dict)
            )
            overlap = len(target_tokens & _tokens(haystack))
            if overlap > best_overlap:
                best = author
                best_overlap = overlap
        return best if best_overlap >= 1 else None

    def authors_by_orcid(self, orcid: str) -> list[dict[str, Any]]:
        """All OpenAlex author records carrying this ORCID (split profiles included)."""
        if not orcid:
            return []
        data = self._get("authors", {"filter": f"orcid:{orcid}", "per-page": 25})
        return data.get("results", []) or []

    def iter_works_by_author(self, author_id: str) -> Iterator[dict[str, Any]]:
        return self.iter_works_by_authors([author_id])

    def iter_works_by_authors(self, author_ids: list[str]) -> Iterator[dict[str, Any]]:
        """Works of any of these authors, 200 per request (one OR filter)."""
        author_id = "|".join(normalize_openalex_id(a) for a in author_ids if normalize_openalex_id(a))
        if not author_id:
            return
        cursor = "*"
        while cursor:
            # Errors propagate: an empty list here would silently erase
            # this professor's collaborations.
            data = self._get(
                "works",
                {
                    "filter": f"author.id:{author_id}",
                    "per-page": 200,
                    "cursor": cursor,
                    # Only what the coauthor graph needs; keeps responses small.
                    # mesh + keywords feed research keywords (keywords.py) at no extra request cost.
                    "select": "id,doi,display_name,publication_year,authorships,mesh,keywords",
                },
            )
            yield from data.get("results", [])
            cursor = (data.get("meta") or {}).get("next_cursor")
