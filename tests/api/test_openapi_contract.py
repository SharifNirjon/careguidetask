"""Contract: docs/openapi.json is a reviewed snapshot of the API.

A failing test means the contract changed. If that was intended, run
`python scripts/export_openapi.py` and commit the diff (reviewers see it).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from scripts.export_openapi import OUTPUT, render

HTTP_METHODS = {"get", "post", "patch", "put", "delete"}


def test_openapi_matches_snapshot(app: FastAPI) -> None:
    assert OUTPUT.exists(), "run python scripts/export_openapi.py"
    assert render(app.openapi()) == OUTPUT.read_text(encoding="utf-8"), (
        "API contract changed: run python scripts/export_openapi.py and commit docs/openapi.json"
    )


def _operations(spec: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    return [
        (path, method, op)
        for path, item in spec["paths"].items()
        for method, op in item.items()
        if method in HTTP_METHODS
    ]


def test_every_route_is_documented(app: FastAPI) -> None:
    spec = app.openapi()
    for path, method, op in _operations(spec):
        where = f"{method.upper()} {path}"
        assert op.get("summary"), where
        assert op.get("tags"), where
        if not path.startswith("/health"):
            assert {"401", "422", "429"} <= set(op["responses"]), where
            assert op.get("security"), f"{where} must require the bearer key"


def test_money_is_never_a_number_in_the_schema(app: FastAPI) -> None:
    schemas = app.openapi()["components"]["schemas"]
    money_fields = {"total", "subtotal", "tax", "amount", "unit_price", "quantity", "tax_rate"}
    for name, schema in schemas.items():
        for field, prop in schema.get("properties", {}).items():
            if field in money_fields:
                text = json.dumps(prop)
                assert '"number"' not in text, f"{name}.{field} is a float in the contract"


def test_snapshot_file_is_valid_json() -> None:
    assert json.loads(Path(OUTPUT).read_text(encoding="utf-8"))["openapi"].startswith("3.")
