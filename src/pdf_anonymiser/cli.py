"""``pdf-anonymise`` — the command-line entry point.

Two subcommands:
    pdf-anonymise serve            run the review web app (uvicorn)
    pdf-anonymise batch ...        process one batch shard (same as the Cloud Run job)
"""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pdf-anonymise", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the review web app")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8080)

    sub.add_parser("batch", help="process one batch shard (passes remaining args through)")

    args, rest = parser.parse_known_args(argv)

    if args.command == "serve":
        import uvicorn

        from .webapp import create_app

        uvicorn.run(create_app(), host=args.host, port=args.port)
        return 0

    if args.command == "batch":
        from .batch_runner import main as batch_main

        return batch_main(rest)

    return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
