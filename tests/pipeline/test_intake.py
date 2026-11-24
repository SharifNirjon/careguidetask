"""File-type detection and intake (storage + DB registration)."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import PipelineSettings
from app.models import Document, DocumentStatus, ProcessingEvent, ProcessingJob
from app.models.enums import ErrorCode
from app.pipeline import filetypes
from app.pipeline.errors import PermanentError
from app.pipeline.intake import ingest_file
from app.pipeline.storage import LocalStorage, validate_key
from app.pipeline.types import FileKind


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("digital_invoice.pdf", FileKind.PDF),
        ("exif_rotated.jpg", FileKind.JPEG),
        ("blurry.png", FileKind.PNG),
        ("three_frames.tiff", FileKind.TIFF),
        ("invoices.xlsx", FileKind.XLSX),
        ("invoice.xls", FileKind.XLS),
        ("semicolon_cp1252.csv", FileKind.CSV),
        ("utf16.csv", FileKind.CSV),
    ],
)
def test_detects_real_type(
    fixture_files: dict[str, Path], settings: PipelineSettings, name: str, kind: FileKind
) -> None:
    path = fixture_files[name]
    assert filetypes.detect(path, filetypes.normalize_extension(name), settings).kind is kind


@pytest.mark.parametrize(
    ("name", "code"),
    [
        ("renamed_exe.pdf", ErrorCode.TYPE_MISMATCH),
        ("zip_bomb.xlsx", ErrorCode.DECOMPRESSION_BOMB),
        ("macros.xlsm", ErrorCode.UNSUPPORTED_TYPE),
    ],
)
def test_rejects_bad_files(
    fixture_files: dict[str, Path], settings: PipelineSettings, name: str, code: ErrorCode
) -> None:
    with pytest.raises(PermanentError) as info:
        filetypes.detect(fixture_files[name], filetypes.normalize_extension(name), settings)
    assert info.value.code is code


def test_macro_content_is_rejected_even_with_xlsx_extension(
    fixture_files: dict[str, Path], settings: PipelineSettings
) -> None:
    with pytest.raises(PermanentError) as info:
        filetypes.detect(fixture_files["macros.xlsm"], "xlsx", settings)
    assert info.value.code is ErrorCode.UNSUPPORTED_TYPE
    assert info.value.details["reason"] == "macro_enabled"


def test_extension_content_disagreement_is_type_mismatch(
    fixture_files: dict[str, Path], settings: PipelineSettings
) -> None:
    with pytest.raises(PermanentError) as info:
        filetypes.detect(fixture_files["blurry.png"], "pdf", settings)
    assert info.value.code is ErrorCode.TYPE_MISMATCH


def test_unknown_extension_trusts_content(fixture_files: dict[str, Path], settings: PipelineSettings) -> None:
    assert filetypes.detect(fixture_files["digital_invoice.pdf"], "", settings).kind is FileKind.PDF


def test_random_binary_is_unsupported(tmp_path: Path, settings: PipelineSettings) -> None:
    path = tmp_path / "blob"
    path.write_bytes(bytes(range(256)) * 8)
    with pytest.raises(PermanentError) as info:
        filetypes.detect(path, "", settings)
    assert info.value.code is ErrorCode.UNSUPPORTED_TYPE


# ------------------------------------------------------------------- intake


def _noop_enqueue(calls: list) -> object:
    return lambda doc_id, job_id: calls.append((doc_id, job_id))


def test_ingest_creates_document_job_and_event(
    db: Session, settings: PipelineSettings, fixture_files: dict[str, Path]
) -> None:
    storage = LocalStorage(settings.storage_root)
    calls: list = []
    data = fixture_files["digital_invoice.pdf"].read_bytes()
    doc = ingest_file(
        io.BytesIO(data),
        "../../etc/passwd.pdf",
        "alice",
        db,
        storage=storage,
        enqueue=_noop_enqueue(calls),
        settings=settings,
    )
    assert doc.status is DocumentStatus.QUEUED
    assert doc.mime_type == "application/pdf" and doc.size_bytes == len(data)
    assert validate_key(doc.storage_key)  # UUID key; the user's filename is not in the path
    assert "passwd" not in doc.storage_key
    assert (settings.storage_root / doc.storage_key).read_bytes() == data
    job = db.scalar(select(ProcessingJob).where(ProcessingJob.document_id == doc.id))
    assert calls == [(doc.id, job.id)]  # enqueued exactly once, after commit
    assert db.scalar(select(func.count()).where(ProcessingEvent.document_id == doc.id)) == 1


def test_duplicate_is_stored_and_linked(
    db: Session, settings: PipelineSettings, fixture_files: dict[str, Path]
) -> None:
    storage = LocalStorage(settings.storage_root)
    data = fixture_files["invoices.xlsx"].read_bytes()
    first = ingest_file(
        io.BytesIO(data), "a.xlsx", None, db, storage=storage, enqueue=_noop_enqueue([]), settings=settings
    )
    second = ingest_file(
        io.BytesIO(data), "b.xlsx", None, db, storage=storage, enqueue=_noop_enqueue([]), settings=settings
    )
    assert second.duplicate_of_document_id == first.id
    assert first.storage_key != second.storage_key


@pytest.mark.parametrize(
    ("payload", "name", "code", "overrides"),
    [
        (b"", "empty.pdf", ErrorCode.EMPTY_FILE, {}),
        (b"%PDF-1.7\n" + b"0" * 4096, "big.pdf", ErrorCode.FILE_TOO_LARGE, {"max_file_size_mb": 0}),
        (b"MZ\x90\x00" + b"\x00" * 100, "invoice.pdf", ErrorCode.TYPE_MISMATCH, {}),
    ],
)
def test_intake_rejections_leave_nothing_behind(
    db: Session, tmp_path: Path, payload: bytes, name: str, code: ErrorCode, overrides: dict
) -> None:
    settings = PipelineSettings.model_construct(
        **{**PipelineSettings(storage_root=tmp_path / "s").model_dump(), **overrides}
    )
    storage = LocalStorage(settings.storage_root)
    calls: list = []
    with pytest.raises(PermanentError) as info:
        ingest_file(
            io.BytesIO(payload),
            name,
            None,
            db,
            storage=storage,
            enqueue=_noop_enqueue(calls),
            settings=settings,
        )
    assert info.value.code is code
    assert calls == []
    assert db.scalar(select(func.count()).select_from(Document)) == 0
    stored = [p for p in (tmp_path / "s").rglob("*") if p.is_file()] if (tmp_path / "s").exists() else []
    assert stored == []


def test_size_limit_is_enforced_while_streaming(db: Session, tmp_path: Path) -> None:
    """The reader aborts as soon as the limit is crossed; it never reads the rest."""

    class Endless(io.RawIOBase):
        reads = 0

        def readinto(self, buffer: bytearray) -> int:  # type: ignore[override]
            Endless.reads += 1
            buffer[:] = b"%" * len(buffer)
            return len(buffer)

        def readable(self) -> bool:
            return True

    settings = PipelineSettings.model_construct(
        **{**PipelineSettings(storage_root=tmp_path / "s").model_dump(), "max_file_size_mb": 1}
    )
    with pytest.raises(PermanentError) as info:
        ingest_file(
            io.BufferedReader(Endless()),
            "x.pdf",
            None,
            db,
            storage=LocalStorage(settings.storage_root),
            enqueue=lambda *_: None,
            settings=settings,
        )
    assert info.value.code is ErrorCode.FILE_TOO_LARGE
    assert Endless.reads < 10
