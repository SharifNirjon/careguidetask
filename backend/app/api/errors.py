"""Domain errors raised by API services. No HTTP here: problems.py maps them."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class FieldError:
    """One field-level problem (RFC 9457 `errors[]` entry)."""

    loc: tuple[str | int, ...]
    msg: str
    type: str = "value_error"

    def to_dict(self) -> dict[str, Any]:
        return {"loc": list(self.loc), "msg": self.msg, "type": self.type}


@dataclass(eq=False)
class DomainError(Exception):
    """Base: a stable machine `code` plus a human `detail` safe to show users."""

    detail: str
    code: str = "error"
    errors: list[FieldError] = field(default_factory=list)
    headers: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__init__(self.detail)


class BadRequest(DomainError):
    pass


class Unauthorized(DomainError):
    pass


class Forbidden(DomainError):
    pass


class NotFound(DomainError):
    pass


class Conflict(DomainError):
    pass


class PreconditionFailed(DomainError):
    """If-Match didn't match the current version (412)."""


class PreconditionRequired(DomainError):
    """A conditional header is mandatory for this write (428)."""


class PayloadTooLarge(DomainError):
    pass


class ValidationFailed(DomainError):
    """Well-formed request, invalid values (422 with field errors)."""


class TooManyRequests(DomainError):
    pass


class NotImplementedYet(DomainError):
    """Contract defined, implementation arrives in a later step (501)."""


class ServiceUnavailable(DomainError):
    pass
