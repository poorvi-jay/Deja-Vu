"""Ask the graph what a new alert reminds it of.

Usage:
    python scripts/find_similar.py --service refund-service ^
        --error "HikariPool-1 - Connection is not available, request timed out after 30000ms." ^
        --tags connection-pool,timeout,database
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dejavu.retrieve import Alert, at_risk_services, find_similar  # noqa: E402
from run_cypher import connect, load_env  # noqa: E402


def parse_args():
    p = argparse.ArgumentParser(description="Find past incidents like this one.")
    p.add_argument("--service", required=True, help="Service that is alerting")
    p.add_argument("--error", required=True, help="Raw error message from the alert")
    p.add_argument("--tags", default="", help="Comma-separated tags, if you have them")
    p.add_argument("--limit", type=int, default=5, help="How many matches to show")
    return p.parse_args()


def main():
    args = parse_args()
    tags = [t.strip() for t in args.tags.split(",") if t.strip()]
    alert = Alert(service=args.service, error=args.error, tags=tags)

    load_env()
    graph = connect()

    print(f"\nAlert on {alert.service}")
    print(f"  raw error:  {alert.error}")
    print(f"  normalised: {alert.signature}")
    if tags:
        print(f"  tags:       {', '.join(tags)}")

    matches = find_similar(graph, alert, limit=args.limit)
    if not matches:
        print("\nNothing in the graph resembles this. It may be genuinely new.")
    else:
        print(f"\n{len(matches)} similar past incident(s):\n")
        for m in matches:
            print(f"  [{m.score:>3}] {m.id}  {m.severity}  {m.service}  ({m.occurred_at[:10]})")
            print(f"        {m.title}")
            print(f"        why: {m.why()}")
            if m.also_shares():
                print(f"        also shares: {m.also_shares()} (context, not a cause)")
            if m.resolution:
                print(f"        fix: {m.resolution}  -- {m.fixed_by}")
            print()

    peers = at_risk_services(graph, alert)
    if peers:
        print("Services sharing a dependency with this one:\n")
        for p in peers:
            via = ", ".join(p["via"])
            count = int(p["past_incidents"])
            note = (
                f"{count} past incident(s) with these tags"
                if count
                else "no history of this - exposed but not yet bitten"
            )
            print(f"  {p['service']:<22} via {via:<16} {note}")
        print()


if __name__ == "__main__":
    main()
