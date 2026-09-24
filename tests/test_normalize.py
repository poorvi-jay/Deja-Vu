import json
from pathlib import Path

import pytest

from dejavu.normalize import normalize

DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "incidents.json"


# Real alerts, as they would actually arrive, paired with the stored
# signature they must normalise to.
@pytest.mark.parametrize(
    "raw, expected",
    [
        (
            "HikariPool-1 - Connection is not available, request timed out after 30000ms.",
            "HikariPool-1 - Connection is not available, request timed out after Nms",
        ),
        (
            "HikariPool-1 - Connection is not available, request timed out after 5000ms",
            "HikariPool-1 - Connection is not available, request timed out after Nms",
        ),
        (
            "RedisCommandTimeoutException: Command timed out after 1 s",
            "RedisCommandTimeoutException: Command timed out after Ns",
        ),
        (
            "  OOMKilled:   container exceeded memory limit  ",
            "OOMKilled: container exceeded memory limit",
        ),
        (
            "Connection marked as broken because of SQLSTATE(08006)",
            "Connection marked as broken because of SQLSTATE(08006)",
        ),
    ],
)
def test_raw_alert_normalises_to_stored_signature(raw, expected):
    assert normalize(raw) == expected


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("failed at 2025-08-20T06:12:00Z", "failed at <TS>"),
        ("failed at 2025-08-20 06:12:00.123+05:30", "failed at <TS>"),
        ("request 3f2a9c1e-8b4d-4e6f-9a1b-2c3d4e5f6a7b failed", "request <UUID> failed"),
        ("object at 0x7f3a2b1c", "object at <HEX>"),
        ("cannot reach 10.0.3.17:5432", "cannot reach <IP>"),
        ("lag is 2000000 messages", "lag is N messages"),
    ],
)
def test_variable_parts_are_replaced(raw, expected):
    assert normalize(raw) == expected


@pytest.mark.parametrize(
    "text",
    ["HikariPool-1", "x509: certificate", "upstream returned 404", "line 1 column 1"],
)
def test_short_identifying_numbers_survive(text):
    assert normalize(text) == text


def test_normalize_is_idempotent():
    raw = "HikariPool-1 timed out after 30000ms at 2025-08-20T06:12:00Z"
    once = normalize(raw)
    assert normalize(once) == once


def test_every_stored_signature_is_already_normalised():
    """The loader will run normalize() on each stored signature before writing
    it. If that changed any of them, the graph would silently gain new
    ErrorSignature nodes that no longer match the data file. This guards it."""
    data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    for inc in data["incidents"]:
        sig = inc["error_signature"]
        assert normalize(sig) == sig, f"{inc['id']}: {sig!r} -> {normalize(sig)!r}"
