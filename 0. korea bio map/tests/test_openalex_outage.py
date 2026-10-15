import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import openalex_client  # noqa: E402
from openalex_client import OpenAlexClient, OpenAlexUnavailable  # noqa: E402


class Resp:
    def __init__(self, code, data=None):
        self.status_code, self._data, self.text, self.headers = code, data, "Application Error", {}

    def json(self):
        return self._data


class Session:
    def __init__(self, codes):
        self.codes = list(codes)
        self.calls = 0

    def get(self, *a, **k):
        self.calls += 1
        code = self.codes.pop(0)
        return Resp(code, {"results": []})


def test_short_outage_is_waited_out(monkeypatch):
    waits = []
    monkeypatch.setattr(openalex_client.time, "sleep", waits.append)
    client = OpenAlexClient()
    client.session = Session([503] * 5 + [200])
    assert client._get("works", {}) == {"results": []}
    assert sum(waits) >= 10 + 30 + 60 + 120


def test_long_outage_stops_the_run(monkeypatch):
    monkeypatch.setattr(openalex_client.time, "sleep", lambda s: None)
    client = OpenAlexClient()
    client.session = Session([503] * 50)
    with pytest.raises(OpenAlexUnavailable):
        client._get("works", {})
    assert client.session.calls == len(openalex_client.OUTAGE_WAITS) + client.max_retries
