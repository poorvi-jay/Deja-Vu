"""Turn a raw error message into a stable signature.

Two occurrences of the same failure never produce identical text: the
timeout is 30000ms one day and 5000ms the next, the request id is different
every time, the timestamp always is. Exact-match lookup on the raw text would
therefore never find anything. normalize() strips out the parts that vary so
the parts that identify the failure are left behind.

The one rule that matters: this SAME function runs on both sides — on stored
signatures when the loader writes them, and on incoming alerts when retrieval
looks them up. If the two sides were normalised differently, they would never
match, however good each rule is on its own.
"""

import re

# Order matters: each rule runs on the output of the one before. Timestamps go
# first because they contain digits that later rules would otherwise chew up
# piecemeal, leaving something like "N-N-NTN:N:NZ".
_RULES = [
    # 2025-08-20T06:12:00Z, 2025-08-20 06:12:00.123+05:30
    (re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"), "<TS>"),
    # 3f2a9c1e-8b4d-4e6f-9a1b-2c3d4e5f6a7b
    (re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"), "<UUID>"),
    # 0x7f3a2b1c (memory addresses, object ids)
    (re.compile(r"\b0x[0-9a-fA-F]+\b"), "<HEX>"),
    # 10.0.3.17, optionally with :5432
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b"), "<IP>"),
    # Durations: 30000ms, 5s, 250 ms. The unit is kept because "Nms" and "Ns"
    # are different failures; only the amount varies.
    (re.compile(r"\b\d+(?:\.\d+)?\s?(ms|s)\b"), r"N\1"),
    # Long bare numbers: request ids, byte counts, offsets, lag figures.
    # Only 6+ digits, so short numbers that ARE identity survive: the 1 in
    # HikariPool-1, the 509 in x509, HTTP 404, "line 1 column 1", and the
    # five-digit SQLSTATE codes Postgres uses to name its error classes.
    (re.compile(r"\b\d{6,}\b"), "N"),
]

_WHITESPACE = re.compile(r"\s+")


def normalize(message: str) -> str:
    """Return the stable signature for a raw error message."""
    text = message.strip()
    for pattern, replacement in _RULES:
        text = pattern.sub(replacement, text)
    text = _WHITESPACE.sub(" ", text)
    # Trailing punctuation is noise: "after 30000ms." and "after 30000ms"
    # are the same error.
    return text.rstrip(" .;:,")
