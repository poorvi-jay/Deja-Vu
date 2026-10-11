"""Load every service and incident from data/incidents.json into FalkorDB.

Usage:
    python scripts/load_incidents.py

Every write is a MERGE, so it is safe to re-run: a second run reports 0 nodes
and 0 relationships created. The work itself lives in dejavu/loader.py, which
the API also uses to seed an empty graph on startup.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Python only searches this script's own folder (scripts/) for imports, so the
# repo root has to be added before the dejavu package can be found.
sys.path.insert(0, str(ROOT))

from dejavu.loader import DataError, load_all  # noqa: E402
# run_cypher.py sits next to this file, so Python can import it directly.
from run_cypher import connect, load_env  # noqa: E402


def main():
    load_env()
    try:
        result = load_all(connect())
    except DataError as exc:
        sys.exit(str(exc))

    print(
        f"Loaded {result['services']} services, {result['incidents']} incidents.\n"
        f"nodes created: {result['nodes_created']}  "
        f"relationships created: {result['relationships_created']}"
    )


if __name__ == "__main__":
    main()
