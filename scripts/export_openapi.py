"""Write docs/openapi.json from the app (the API contract under review).

    python scripts/export_openapi.py            # write
    python scripts/export_openapi.py --check    # exit 1 if docs/openapi.json is stale

tests/api/test_openapi_contract.py fails when the file is stale, so every
contract change shows up in the diff of a pull request.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

OUTPUT = ROOT / "docs" / "openapi.json"


def render(spec: dict[str, Any]) -> str:
    return json.dumps(spec, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def current_spec() -> dict[str, Any]:
    """No connection is opened: engines and Redis clients connect lazily, so a
    placeholder DATABASE_URL is enough to build the app (CI needs no database)."""
    os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://openapi@localhost:5432/openapi")
    from app.api.app import create_app
    from app.config import ApiSettings

    spec: dict[str, Any] = create_app(ApiSettings(docs_enabled=True)).openapi()
    return spec


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    text = render(current_spec())
    if args.check:
        stale = not OUTPUT.exists() or OUTPUT.read_text(encoding="utf-8") != text
        print(
            "docs/openapi.json is stale: run scripts/export_openapi.py"
            if stale
            else "docs/openapi.json is current"
        )
        return int(stale)
    OUTPUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUTPUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
