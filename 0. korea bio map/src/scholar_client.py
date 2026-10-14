from __future__ import annotations

import os
import re
from difflib import SequenceMatcher
from typing import Any

import requests

from config import REQUEST_TIMEOUT


SERPAPI_KEY = os.getenv("SERPAPI_KEY", "").strip()
SCHOLAR_LOOKUP_LIMIT = int(os.getenv("SCHOLAR_LOOKUP_LIMIT", "60"))


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").casefold())


def _name_similarity(a: str, b: str) -> float:
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    return SequenceMatcher(None, na, nb).ratio()


class ScholarClient:
    """Google Scholar profile lookup via SerpApi.

    Direct scraping from GitHub Actions is intentionally avoided because it is
    brittle and frequently triggers bot/CAPTCHA protection. If SERPAPI_KEY is
    absent, the pipeline simply falls back to OpenAlex current affiliations.
    """

    def __init__(self) -> None:
        self.api_key = SERPAPI_KEY
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "AboutBio-KoreaBioMap/0.2"})

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def search_profile(
        self,
        name: str,
        known_institutions: str = "",
    ) -> dict[str, Any] | None:
        if not self.enabled or not name:
            return None

        try:
            response = self.session.get(
                "https://serpapi.com/search.json",
                params={
                    "engine": "google_scholar_profiles",
                    "mauthors": name,
                    "hl": "en",
                    "api_key": self.api_key,
                },
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError):
            # Never fail the whole GitHub Actions run because Scholar lookup
            # is rate-limited, unavailable, or the SerpApi quota is exhausted.
            return None

        # SerpApi may return an error payload with HTTP 200.
        if payload.get("error"):
            return None

        profiles = payload.get("profiles") or []
        if not profiles:
            return None

        known = (known_institutions or "").casefold()
        ranked = []
        for profile in profiles[:10]:
            sim = _name_similarity(name, profile.get("name", ""))
            affiliation = (profile.get("affiliations") or "").casefold()
            overlap_bonus = 0.0
            if known and affiliation:
                tokens = {
                    token
                    for token in re.findall(r"[a-z]{4,}", known)
                    if token not in {"university", "college", "institute", "hospital"}
                }
                if any(token in affiliation for token in tokens):
                    overlap_bonus = 0.2
            ranked.append((sim + overlap_bonus, sim, profile))

        ranked.sort(key=lambda item: item[0], reverse=True)
        _, name_sim, best = ranked[0]
        if name_sim < 0.82:
            return None
        return best

    def author_article_titles(self, scholar_id: str, limit: int = 8) -> list[str]:
        """Titles of a Google Scholar profile's most cited articles (SerpApi google_scholar_author)."""
        if not self.enabled or not scholar_id:
            return []
        try:
            response = self.session.get(
                "https://serpapi.com/search.json",
                params={"engine": "google_scholar_author", "author_id": scholar_id, "num": limit,
                        "sort": "citedby", "api_key": self.api_key},
                timeout=REQUEST_TIMEOUT,
            )
            data = response.json() if response.status_code == 200 else {"error": f"HTTP {response.status_code}"}
        except (requests.RequestException, ValueError) as exc:
            print(f"[scholar] profile {scholar_id}: {type(exc).__name__}", flush=True)
            return []
        if data.get("error"):
            print(f"[scholar] profile {scholar_id}: {str(data['error'])[:160]}", flush=True)
            return []
        return [str(a.get("title", "")) for a in data.get("articles", []) or [] if a.get("title")][:limit]
