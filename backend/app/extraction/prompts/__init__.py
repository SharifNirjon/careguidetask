"""Versioned prompt files. The version string is stored on every extraction.

Changing any file's wording = a new version (copy to ..._v2.*), so stored
extractions always say exactly which instructions produced them, and the
response cache never serves an answer produced by older instructions.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from importlib import resources
from string import Template
from typing import Final

PROMPT_VERSION: Final = "invoice_extraction_v1"


class Scope(StrEnum):
    FULL = "full"
    HEADER = "header"
    LINE_ITEMS = "line_items"


@dataclass(frozen=True, slots=True)
class PromptTemplate:
    version: str
    system: str
    user: Template
    scopes: dict[Scope, str]

    def render_user(self, scope: Scope, document: str) -> str:
        return self.user.substitute(scope=self.scopes[scope].strip(), document=document)


def _read(name: str) -> str:
    return resources.files(__package__).joinpath(name).read_text(encoding="utf-8")


def load_prompt(version: str = PROMPT_VERSION) -> PromptTemplate:
    return PromptTemplate(
        version=version,
        system=_read(f"{version}.system.md").strip(),
        user=Template(_read(f"{version}.user.md")),
        scopes={scope: _read(f"{version}.scope.{scope.value}.md") for scope in Scope},
    )
