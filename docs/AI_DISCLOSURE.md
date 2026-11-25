# AI-assisted development: disclosure and prompts

## Summary

This project was built with substantial AI assistance. **Claude Code** (Anthropic's
coding agent, Claude Opus model) wrote most of the code, tests and documentation, working
step by step from detailed engineering prompts written by the project author. The author
set the requirements and architecture constraints, reviewed the output, made the
decisions the agent asked about (scope, trade-offs, when to commit), and ran and checked
the system.

The application itself also uses AI at runtime: an LLM (via OpenRouter's free tier)
extracts invoice fields from documents, and an embedding model powers semantic search.
Both are described below.

## 1. AI used to build the project

| What | Tool | Role of the AI | Role of the author |
|---|---|---|---|
| Code (backend, pipeline, frontend) | Claude Code | Wrote the implementation from the step prompts | Wrote the prompts, reviewed, answered design questions |
| Tests | Claude Code | Wrote unit, integration and e2e tests; fixed the bugs they exposed | Reviewed; decided scope |
| Documentation | Claude Code | Drafted the docs in `docs/` and this file | Reviewed |
| Debugging and operations | Claude Code | Diagnosed failures (Docker memory, IPv6 port forwarding, disk space) and proposed fixes | Approved actions that touch the machine (restarting Docker, cache locations) |

### How the work was steered

The project was built in steps. Each step had a written prompt with a role, the
context, the scope, hard constraints (especially security: secrets only from
environment variables, document text treated as untrusted, no secrets in logs or in
the browser), a working style ("say so before coding if anything is risky") and
acceptance criteria. The agent was told to commit or push only when explicitly asked.

The prompts, verbatim, are in [`docs/prompts/`](prompts/):

| Step | Prompt |
|---|---|
| 2. Database layer | [step-02-database.md](prompts/step-02-database.md) |
| 3. Ingestion and parsing pipeline (PDF, OCR, images, Excel, CSV) | [step-03-ingestion-pipeline.md](prompts/step-03-ingestion-pipeline.md) |
| 4. AI extraction layer and LLM gateway | [step-04-ai-extraction.md](prompts/step-04-ai-extraction.md) |
| 5. Validation layer | [step-05-validation.md](prompts/step-05-validation.md) |
| 6. Public REST API | not retained (see below) |
| 7. Search (keyword, semantic, hybrid) | [step-07-search.md](prompts/step-07-search.md) |
| 8. Review UI (frontend) | [step-08-review-ui.md](prompts/step-08-review-ui.md) |
| 9. Testing strategy | [step-09-testing.md](prompts/step-09-testing.md) |
| 10. Evaluation | [step-10-evaluation.md](prompts/step-10-evaluation.md) |

Notes on the record:
- **Step 1** (repository set-up) and **Step 6** (REST API) prompts are not
  reproduced: their text was lost when the agent's long session history was
  summarized. The REST API's requirements are documented in [api.md](api.md).
- One prompt (Step 4) contained the author's OpenRouter API key; it is replaced by
  `[redacted]` here and the key should be rotated. The key was otherwise kept only
  in the gitignored `.env`.
- Short follow-up messages (answers to the agent's questions, "commit", "continue")
  are not listed.

### What to keep in mind when reviewing

- The code was generated quickly and reviewed, but not written line by line by a
  human. The automated tests, the type checkers (mypy strict, TypeScript strict) and
  the linters are the main safety net; their results are in
  [test-results/](test-results/).
- Where the agent found its own earlier code wrong (for example the login form
  falling back to a GET request with the password in the URL when JavaScript failed
  to load), it fixed the bug and added a test.

## 2. AI used inside the application (runtime)

| Use | Model (configurable) | Where |
|---|---|---|
| Invoice field extraction from PDF text / OCR text | `nvidia/nemotron-3-ultra-550b-a55b:free` via OpenRouter, with fallbacks | `backend/app/llm/`, `backend/app/extraction/` |
| Embeddings for semantic search | `nvidia/nemotron-3.5-embed-8b:free` (1024-dim), with fallback spaces | `backend/app/llm/embeddings.py`, `backend/app/search/` |

### Runtime prompts

Prompts are files, versioned (`invoice_extraction_v1`), and the version is stored on
every extraction:
- [`backend/app/extraction/prompts/`](../backend/app/extraction/prompts/): system
  prompt, user template, and scopes for full / header / line-item requests.
- [`backend/app/llm/prompts/`](../backend/app/llm/prompts/): the JSON-repair prompt,
  the schema instruction for models without structured output, and the probe prompt.
- The rendered prompt is pinned by a golden-file test
  (`tests/extraction/golden/invoice_extraction_v1.full.txt`).

### Safeguards around the model

- **No invented values.** Every extracted field must cite evidence that is found in
  the document text ("grounding"); values that can't be grounded are set to null and
  recorded as `rejected_value`, and validation raises `UNGROUNDED_VALUE`.
- **Untrusted input.** Document text is placed inside a delimited block; delimiter
  look-alikes in the document are neutralized so the text can't close the block, and
  the system prompt tells the model to treat the block as data (tested with injection
  text).
- **Deterministic where possible.** Spreadsheets and CSVs are extracted by rules, with
  zero LLM calls. Temperature is 0 and a strict JSON schema is used.
- **Human review.** Low confidence, failed checks and arithmetic mismatches send an
  invoice to a reviewer; nothing is auto-approved with an open error.
- **Privacy.** Free endpoints may log traffic, so only synthetic documents were used
  for development, tests and evaluation.
- **Secrets.** The API key is read only from the environment (`OPENROUTER_API_KEY`),
  never logged, never returned by the API, never sent to the browser; tests assert
  this.

Details: [extraction.md](extraction.md), [validation.md](validation.md),
[search.md](search.md).
