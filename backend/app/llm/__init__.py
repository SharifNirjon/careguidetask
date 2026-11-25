"""LLM gateway over OpenRouter. Import from here; nothing else leaks the SDK."""

from app.llm.chat import ChatGateway, ChatResult, StructuredChat
from app.llm.embeddings import EmbeddingGateway, EmbeddingResult
from app.llm.errors import (
    EmbeddingDimensionError,
    ExtractionParseError,
    LLMAuthError,
    LLMError,
    LLMUnavailableError,
    QuotaExhaustedError,
)
from app.llm.types import ChatMessage, OutputMode, TokenUsage

__all__ = [
    "ChatGateway",
    "ChatMessage",
    "ChatResult",
    "EmbeddingDimensionError",
    "EmbeddingGateway",
    "EmbeddingResult",
    "ExtractionParseError",
    "LLMAuthError",
    "LLMError",
    "LLMUnavailableError",
    "OutputMode",
    "QuotaExhaustedError",
    "StructuredChat",
    "TokenUsage",
]
