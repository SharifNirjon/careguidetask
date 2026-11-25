"""Strong ETags over the serialized body: correct by construction, cheap enough
for polling. If-None-Match match -> 304 with no body."""

from __future__ import annotations

import hashlib
from typing import Final

from fastapi import Request, Response
from pydantic import BaseModel

ETAG_HASH_CHARS: Final = 32


def etag_for(body: bytes) -> str:
    return f'"{hashlib.sha256(body).hexdigest()[:ETAG_HASH_CHARS]}"'


def _matches(header: str | None, etag: str) -> bool:
    if not header:
        return False
    candidates = {c.strip().removeprefix("W/") for c in header.split(",")}
    return "*" in candidates or etag in candidates


def conditional_json(
    request: Request, model: BaseModel, *, status_code: int = 200, etag_basis: BaseModel | None = None
) -> Response:
    """`etag_basis`: the same content minus fields that change on every read
    (a running job's elapsed time), so polling an unchanged document still gets 304."""
    body = model.model_dump_json().encode()
    etag = etag_for((etag_basis or model).model_dump_json().encode())
    headers = {"ETag": etag, "Cache-Control": "private, no-cache"}
    if _matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers=headers)
    return Response(content=body, status_code=status_code, media_type="application/json", headers=headers)
