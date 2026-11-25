"""FastAPI dependencies: container, DB session, principal, role + rate-limit guards.

Guard order is fixed: authenticate (401) -> authorize role (403) -> rate
limit (429). An unauthenticated flood therefore never touches a key's budget.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.api.auth import Principal
from app.api.container import Container
from app.api.errors import Forbidden, TooManyRequests
from app.api.ratelimit import RateClass
from app.api.services.auth import AuthService
from app.models.enums import ApiRole

bearer = HTTPBearer(auto_error=False, description="API key: `Authorization: Bearer cg_…`")


def get_container(request: Request) -> Container:
    container: Container = request.app.state.container
    return container


ContainerDep = Annotated[Container, Depends(get_container)]


def get_session(container: ContainerDep) -> Iterator[Session]:
    """One session per request; anything not committed by the service is rolled back."""
    session = container.session_factory()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


SessionDep = Annotated[Session, Depends(get_session)]


def get_principal(
    request: Request,
    container: ContainerDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> Principal:
    token = credentials.credentials if credentials and credentials.scheme.lower() == "bearer" else None
    with container.session_factory() as session:
        principal = AuthService(session).authenticate(token)
    request.scope.setdefault("state", {})["principal_name"] = principal.name  # for the access log
    return principal


def _limit_for(container: Container, rate_class: RateClass) -> int:
    api = container.api
    return {RateClass.UPLOAD: api.upload_rpm, RateClass.WRITE: api.write_rpm, RateClass.READ: api.read_rpm}[
        rate_class
    ]


def guard(role: ApiRole, rate_class: RateClass) -> Callable[..., Principal]:
    def dependency(
        principal: Annotated[Principal, Depends(get_principal)], container: ContainerDep
    ) -> Principal:
        if not principal.can(role):
            raise Forbidden(f"This action requires the {role.value} role.", code="forbidden")
        retry_after = container.rate_limiter.hit(
            str(principal.key_id), rate_class, _limit_for(container, rate_class)
        )
        if retry_after:
            raise TooManyRequests(
                "Rate limit exceeded.", code="rate_limited", headers={"Retry-After": str(retry_after)}
            )
        return principal

    return dependency


Viewer = Annotated[Principal, Depends(guard(ApiRole.VIEWER, RateClass.READ))]
ReviewerWrite = Annotated[Principal, Depends(guard(ApiRole.REVIEWER, RateClass.WRITE))]
ReviewerUpload = Annotated[Principal, Depends(guard(ApiRole.REVIEWER, RateClass.UPLOAD))]
AdminRead = Annotated[Principal, Depends(guard(ApiRole.ADMIN, RateClass.READ))]
AdminWrite = Annotated[Principal, Depends(guard(ApiRole.ADMIN, RateClass.WRITE))]
