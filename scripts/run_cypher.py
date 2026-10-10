"""Run a .cypher file against the FalkorDB instance.

Usage:
    python scripts/run_cypher.py cypher/01_first_incident.cypher
    python scripts/run_cypher.py --query "MATCH (n) RETURN count(n)"
"""

import os
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

from falkordb import FalkorDB

ROOT = Path(__file__).resolve().parent.parent


def load_env(env_file: Path = None) -> bool:
    """Read .env into os.environ if the file exists. Returns whether it did.

    A missing .env is NOT an error. Locally the file is how you configure
    things; on a deployed host there is no file — .env is gitignored — and the
    platform sets real environment variables instead. An earlier version of
    this exited when the file was absent, which made the deployed service
    report "No .env file found" while its env vars were sitting right there,
    unread.

    Real environment variables take precedence over the file, which is the
    usual precedence and means a deployed value can never be shadowed by a
    stray committed default.

    Parsed by hand rather than with python-dotenv so there is one less
    dependency. Blank lines and #-comments are skipped.
    """
    env_file = env_file or ROOT / ".env"
    if not env_file.exists():
        return False
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())
    return True


def connect():
    """Open a connection and return the graph we store incidents in.

    Accepts either FALKOR_URL (the whole connection string, as the dashboard
    shows it) or the four separate FALKOR_HOST/PORT/USER/PASSWORD values.
    """
    url = os.environ.get("FALKOR_URL", "").strip()
    if url:
        # falkor://user:password@host:port  ->  the four parts we need.
        parsed = urlparse(url)
        if not parsed.hostname or not parsed.port:
            sys.exit(
                f"FALKOR_URL does not look right: {url}\n"
                "Expected something like falkor://falkordb:PASSWORD@host.falkordb.io:61517"
            )
        db = FalkorDB(
            host=parsed.hostname,
            port=parsed.port,
            username=unquote(parsed.username or ""),
            password=unquote(parsed.password or ""),
        )
    else:
        missing = [
            k
            for k in ("FALKOR_HOST", "FALKOR_PORT", "FALKOR_USER", "FALKOR_PASSWORD")
            if not os.environ.get(k)
        ]
        if missing:
            # Names both places on purpose: locally this means .env, on a
            # deployed host it means the platform's environment variables.
            sys.exit(
                "No FalkorDB connection configured.\n"
                "Set FALKOR_URL to the connection string from the dashboard "
                f"(or set {', '.join(missing)}) — in .env locally, or as "
                "environment variables on your host."
            )
        db = FalkorDB(
            host=os.environ["FALKOR_HOST"],
            port=int(os.environ["FALKOR_PORT"]),
            username=os.environ["FALKOR_USER"],
            password=os.environ["FALKOR_PASSWORD"],
        )
    return db.select_graph(os.environ.get("FALKOR_GRAPH", "dejavu"))


def strip_comments(text: str) -> str:
    """Remove // comment lines.

    Our .cypher files are heavily commented for teaching. FalkorDB can handle
    comments, but stripping them keeps the error messages readable when a
    query fails.
    """
    keep = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("//") or not stripped:
            continue
        keep.append(line)
    return "\n".join(keep)


def main():
    load_env()

    args = sys.argv[1:]
    if not args:
        sys.exit(__doc__)

    if args[0] == "--query":
        cypher = args[1]
        label = "inline query"
    else:
        path = ROOT / args[0]
        if not path.exists():
            sys.exit(f"No such file: {path}")
        cypher = strip_comments(path.read_text())
        label = args[0]

    graph = connect()
    print(f"Running {label} ...\n")

    result = graph.query(cypher)

    # result.header is the column names, result.result_set the rows.
    if result.header:
        print(" | ".join(str(col[1]) for col in result.header))
        print("-" * 40)
    for row in result.result_set:
        print(" | ".join(str(cell) for cell in row))

    print(
        f"\nnodes created: {result.nodes_created}  "
        f"relationships created: {result.relationships_created}  "
        f"properties set: {result.properties_set}"
    )


if __name__ == "__main__":
    main()
