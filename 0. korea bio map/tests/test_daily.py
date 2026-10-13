import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import main  # noqa: E402
from openalex_client import OpenAlexUnavailable  # noqa: E402


def _patch(monkeypatch, calls, fail_at=""):
    def step(name, result=None):
        def run(*a, **k):
            calls.append(name)
            if name == fail_at:
                raise OpenAlexUnavailable("daily budget")
            return result if result is not None else []
        return run
    monkeypatch.setattr(main, "collect_alumni", step("alumni"))
    monkeypatch.setattr(main, "run_all", step("map"))
    monkeypatch.setattr(main, "discover_departments", step("departments"))
    monkeypatch.setattr(main, "scrape_faculty_v2", step("faculty"))
    monkeypatch.setattr(main, "resolve_faculty_identities_v2", step("identities"))
    monkeypatch.setattr(main, "import_verified_faculty_v2", step("import", 0))
    monkeypatch.setattr(main, "discover_by_institution", step("institutions", 0))
    monkeypatch.setattr(main, "fill_korean_names", step("names", 0))


def test_map_is_refreshed_before_expansion(monkeypatch):
    calls = []
    _patch(monkeypatch, calls)
    main.daily(object())
    assert calls == ["alumni", "map", "departments", "faculty", "identities", "import", "institutions", "names"]


def test_spent_budget_stops_expansion_but_not_the_map(monkeypatch):
    calls = []
    _patch(monkeypatch, calls, fail_at="identities")
    main.daily(object())
    assert calls == ["alumni", "map", "departments", "faculty", "identities"]


def test_alumni_failure_does_not_stop_the_map(monkeypatch):
    calls = []
    _patch(monkeypatch, calls, fail_at="alumni")
    main.daily(object())
    assert calls[:2] == ["alumni", "map"]


def test_deadline_skips_remaining_expansion(monkeypatch):
    calls = []
    _patch(monkeypatch, calls)
    monkeypatch.setattr(main, "EXPANSION_DEADLINE_MIN", -1)
    main.daily(object())
    assert calls == ["alumni", "map"]
