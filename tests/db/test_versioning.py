"""Versioned extractions and optimistic locking (needs real, committed transactions)."""

from __future__ import annotations

import threading
import time
from decimal import Decimal

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.exc import NoResultFound
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from app.db.versioning import add_extraction_version
from app.models import Extraction, ReviewStatus
from tests.db.factories import make_document


def _versions(session: Session, document_id) -> list[tuple[int, bool]]:
    return session.execute(
        select(Extraction.version, Extraction.is_current)
        .where(Extraction.document_id == document_id, Extraction.invoice_index == 0)
        .order_by(Extraction.version)
    ).all()


def test_new_version_flips_is_current(db: Session) -> None:
    doc = make_document(db)
    v1 = add_extraction_version(db, document_id=doc.id, invoice_index=0, unit_start=0,
                                unit_end=0, total=Decimal("10"))
    v2 = add_extraction_version(db, document_id=doc.id, invoice_index=0, unit_start=0,
                                unit_end=0, total=Decimal("11"))
    v3 = add_extraction_version(db, document_id=doc.id, invoice_index=0, unit_start=0,
                                unit_end=1, total=Decimal("12"))
    assert [(v.version, v.is_current) for v in (v1, v2, v3)] == [(1, False), (2, False), (3, True)]
    assert _versions(db, doc.id) == [(1, False), (2, False), (3, True)]
    assert v1.lock_version == 2  # the flip went through the ORM and bumped the lock


def test_unknown_document_raises(db: Session) -> None:
    import uuid

    with pytest.raises(NoResultFound):
        add_extraction_version(db, document_id=uuid.uuid4(), invoice_index=0,
                               unit_start=0, unit_end=0)


def test_optimistic_locking_detects_concurrent_review(committed_engine: Engine) -> None:
    with Session(committed_engine) as s:
        doc = make_document(s)
        ext = add_extraction_version(s, document_id=doc.id, invoice_index=0,
                                     unit_start=0, unit_end=0)
        s.commit()
        ext_id = ext.id

    with Session(committed_engine) as alice, Session(committed_engine) as bob:
        a = alice.get(Extraction, ext_id)
        b = bob.get(Extraction, ext_id)
        assert a is not None and b is not None and a.lock_version == b.lock_version == 1

        a.review_status = ReviewStatus.APPROVED
        a.reviewed_by = "alice"
        alice.commit()

        b.review_status = ReviewStatus.REJECTED
        b.reviewed_by = "bob"
        with pytest.raises(StaleDataError):
            bob.commit()  # bob's UPDATE ... WHERE lock_version = 1 matches 0 rows

    with Session(committed_engine) as s:
        final = s.get(Extraction, ext_id)
        assert final is not None
        assert (final.review_status, final.reviewed_by, final.lock_version) == (
            ReviewStatus.APPROVED, "alice", 2,
        )


def test_concurrent_reprocessing_is_serialized(committed_engine: Engine) -> None:
    """Two workers re-extracting the same invoice: the second waits, then gets v3."""
    with Session(committed_engine) as s:
        doc = make_document(s)
        add_extraction_version(s, document_id=doc.id, invoice_index=0, unit_start=0, unit_end=0)
        s.commit()
        doc_id = doc.id

    first = Session(committed_engine)
    add_extraction_version(first, document_id=doc_id, invoice_index=0, unit_start=0, unit_end=0)
    # `first` now holds the document lock without committing.

    errors: list[BaseException] = []
    done = threading.Event()

    def second_worker() -> None:
        try:
            with Session(committed_engine) as second:
                add_extraction_version(second, document_id=doc_id, invoice_index=0,
                                       unit_start=0, unit_end=0)
                second.commit()
        except BaseException as exc:
            errors.append(exc)
        finally:
            done.set()

    t = threading.Thread(target=second_worker)
    t.start()
    time.sleep(0.5)
    assert not done.is_set(), "second worker should be blocked on the document lock"
    first.commit()
    first.close()
    t.join(timeout=10)

    assert errors == []
    with Session(committed_engine) as s:
        assert _versions(s, doc_id) == [(1, False), (2, False), (3, True)]
