"""Root-cause families, derived from tags.

The incident data has no `family` field, deliberately — a family is a judgement
about what kind of failure something was, and tags are the evidence for it. The
grouping is therefore a rule, not stored data: the first family whose marker
tags an incident carries wins.

This lives in one module because three things need the same answer and they
must not disagree: the API's incident catalogue, the evaluation harness, and
the advisories (whose identity includes the family, so a second definition
would silently produce duplicate advisories for one exposure).

Order matters. INC-023 is tagged both `deploy` and `connection-pool`; the pool
is what ran out, so connection-pool is checked first.
"""

from typing import Iterable, List, Tuple

FAMILIES: List[Tuple[str, set]] = [
    ("Connection pool", {"connection-pool"}),
    ("Kafka consumer lag", {"kafka", "consumer-lag"}),
    ("TLS and certificates", {"tls", "certificate"}),
    ("Memory and OOM", {"memory", "oom"}),
    ("Cache and Redis", {"cache", "redis"}),
    ("Deploy and config", {"deploy", "config", "feature-flag"}),
]

OTHER = "Other"

# The slug is what goes in an advisory id, so it has to be stable: renaming a
# family's display text must not orphan every advisory already raised under it.
SLUGS = {
    "Connection pool": "connection-pool",
    "Kafka consumer lag": "kafka",
    "TLS and certificates": "tls",
    "Memory and OOM": "memory",
    "Cache and Redis": "cache",
    "Deploy and config": "deploy",
    OTHER: "other",
}


def family_of(tags: Iterable[str]) -> str:
    """The display name of the family these tags indicate."""
    tags = set(tags or ())
    for name, markers in FAMILIES:
        if markers & tags:
            return name
    return OTHER


def slug_of(tags: Iterable[str]) -> str:
    """The stable identifier for the family these tags indicate."""
    return SLUGS[family_of(tags)]
