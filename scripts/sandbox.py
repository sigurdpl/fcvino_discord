#!/usr/bin/env python3
"""Run the web app against a throwaway copy of the database.

    python scripts/sandbox.py              # fresh copy, then serve it on :8001
    python scripts/sandbox.py --keep       # carry on with last time's sandbox
    python scripts/sandbox.py --copy-only  # just make the copy

The same thing as `scripts/serve.py --sandbox`, which is where the work now
lives. This stays because it is the command already in use and in the README,
and because "the sandbox" is what the thing is called out loud.

Why a sandbox at all: driving an evening end to end means Start, everyone's
cards, and Close. Two of those come back — deleting a test event takes its
bottles and its votes with it, by cascade. Close copies bottles into `wines`
and cards into `wine_ratings`, and undoing that is hand work. So the app gets
pointed somewhere else entirely.

Everything about how is documented in serve.py: the copy goes through sqlite
rather than the filesystem, a fresh one is taken unless `--keep`, and Access is
forced off unless `--behind-cloudflare`.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.serve import (  # noqa: E402,F401
    DEFAULT_SANDBOX,
    WouldClobberTheRealThing,
    copy_database,
    sandbox_env,
    serve,
)
from scripts.serve import main as _serve  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    """Everything reached through here is a sandbox, so the choice is made."""
    return _serve(["--sandbox", *(argv if argv is not None else sys.argv[1:])])


if __name__ == "__main__":
    raise SystemExit(main())
