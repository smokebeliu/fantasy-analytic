"""Export the OpenAPI schema for the user REST API (development-plan step 9).

The schema is generated from the live FastAPI app (``create_app``) so it always
reflects the real request/response models. It never touches the database — the
engine is created lazily and no request is served — so it runs offline in CI.

Usage::

    PYTHONPATH=src python3 -m fantasy_analytics.openapi_cli --output docs/openapi.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from .api import create_app


def build_openapi() -> dict[str, Any]:
    """Return the OpenAPI document for the API without any DB access."""
    app = create_app(
        database_url="postgresql+psycopg://unused:unused@localhost:5432/unused",
        spawn_worker=lambda job_id: None,
    )
    return app.openapi()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="fantasy-openapi",
        description="Export the Fantasy Analytics OpenAPI schema.",
    )
    parser.add_argument(
        "--output",
        default="docs/openapi.json",
        help="File to write the OpenAPI JSON to (default: docs/openapi.json)",
    )
    args = parser.parse_args(argv)

    schema = build_openapi()
    text = json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output == "-":
        sys.stdout.write(text)
    else:
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"OpenAPI schema written to {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
