"""manifest.jsonl schema: one line per dataset file, validated on load."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

DATASET_DIR = Path(__file__).parent / "dataset"
MANIFEST = DATASET_DIR / "manifest.jsonl"

Category = Literal[
    "digital_pdf",
    "scanned_pdf",
    "image",
    "spreadsheet",
    "csv",
    "multi_invoice_pdf",
    "planted_error",
    "invalid",
]
Split = Literal["dev", "test"]


class ExpectedLine(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: str
    quantity: str
    unit_price: str
    amount: str


class ExpectedInvoice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    invoice_number: str | None
    invoice_date: str | None
    supplier_name: str | None
    customer_name: str | None
    subtotal: str | None
    tax: str | None
    total: str | None
    currency: str | None
    line_items: list[ExpectedLine] = Field(default_factory=list)


class Expected(BaseModel):
    model_config = ConfigDict(extra="forbid")
    invoices: list[ExpectedInvoice] = Field(default_factory=list)


class ManifestEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    split: Split
    format: Literal["pdf", "png", "jpg", "tiff", "xlsx", "xls", "csv", "bin"]
    category: Category
    layout: str | None = None
    degradation: Literal["none", "mild", "medium", "heavy"] = "none"
    expected: Expected = Field(default_factory=Expected)
    expected_error_code: str | None = None
    expected_rule_codes: list[str] = Field(default_factory=list)
    group: str | None = None  # files that must stay in one split (duplicate / cross-doc pairs)
    llm_requests: int = Field(ge=0)  # planned extraction requests (0 for spreadsheets and bad files)
    notes: str = ""

    @field_validator("file")
    @classmethod
    def _relative(cls, value: str) -> str:
        if "/" in value or "\\" in value or value.startswith("."):
            raise ValueError("file must be a bare file name inside eval/dataset/files")
        return value

    @property
    def valid(self) -> bool:
        """Valid documents are expected to be processed; invalid ones rejected."""
        return self.expected_error_code is None


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_manifest(path: Path = MANIFEST, *, verify_files: bool = True) -> list[ManifestEntry]:
    entries = [ManifestEntry.model_validate_json(line) for line in path.read_text("utf-8").splitlines() if line]
    names = [e.file for e in entries]
    if len(names) != len(set(names)):
        raise ValueError("duplicate file names in manifest")
    if verify_files:
        for entry in entries:
            actual = sha256_of(path.parent / "files" / entry.file)
            if actual != entry.sha256:
                raise ValueError(f"{entry.file}: sha256 mismatch (dataset changed?)")
    return entries


def write_manifest(entries: Iterable[ManifestEntry], path: Path = MANIFEST) -> None:
    lines = [json.dumps(e.model_dump(mode="json"), ensure_ascii=False, sort_keys=True) for e in entries]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
