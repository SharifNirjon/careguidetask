"""Run the REAL system end to end on the evaluation dataset.

    python -m eval.run_eval --split dev [--limit N] [--category scanned_pdf] [--no-cache]
    python -m eval.run_eval --resume eval/results/<run>          # continue after a quota cut

Needs the docker compose stack (api, worker, db, redis) and EVAL_API_KEY (an
admin key; `python -m eval.run_eval --create-key` writes one into .env). Every
file is uploaded through the public API and polled until it reaches a
terminal state; per-file results are appended to predictions.jsonl, so an
interrupted run continues where it stopped. Nothing here computes metrics:
eval/analyze.py does that from the files this writes.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import platform
import re
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Final

from eval.manifest import DATASET_DIR, ManifestEntry, load_manifest

ROOT: Final = Path(__file__).resolve().parents[1]
RESULTS: Final = Path(__file__).parent / "results"
ENV_FILE: Final = ROOT / ".env"
TERMINAL: Final = frozenset({"completed", "needs_review", "failed"})
POLL_S: Final = 3.0
DOC_TIMEOUT_S: Final = 45 * 60  # generous: quota deferral waits for capacity
RETRY_FACTOR: Final = 1.15  # JSON repair / transient retries, measured on dev runs


# ------------------------------------------------------------------ config


def read_env(path: Path = ENV_FILE) -> dict[str, str]:
    env: dict[str, str] = {}
    if path.exists():
        for line in path.read_text("utf-8").splitlines():
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                env[key.strip()] = value.strip()
    return env


def compose(*args: str, stdin: str | None = None, check: bool = True) -> str:
    out = subprocess.run(
        ["docker", "compose", *args], cwd=ROOT, input=stdin, capture_output=True, text=True, check=False
    )
    if check and out.returncode != 0:
        raise RuntimeError(f"docker compose {' '.join(args[:3])} failed: {out.stderr.strip()[-400:]}")
    return out.stdout


def sql_json(query: str) -> Any:
    """Run a read-only query in the db container; the query must return one json value."""
    out = compose("exec", "-T", "db", "psql", "-U", "careguide", "-d", "careguide", "-At", "-c", query)
    text = out.strip()
    return json.loads(text) if text else None


def create_key() -> None:
    """Admin key for the runner (soft-deletes earlier eval uploads). Written to .env, never printed."""
    out = compose("exec", "-T", "api", "python", "-m", "app.cli", "api-key", "create", "--name", "eval",
                  "--role", "admin")  # fmt: skip
    key = next(line.split(":", 1)[1].strip() for line in out.splitlines() if line.startswith("key:"))
    lines = [ln for ln in ENV_FILE.read_text("utf-8").splitlines() if not ln.startswith("EVAL_API_KEY=")]
    lines.append(f"EVAL_API_KEY={key}")
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("wrote EVAL_API_KEY to .env")


# --------------------------------------------------------------------- http


class Api:
    def __init__(self, base: str, key: str) -> None:
        self.base = base.rstrip("/")
        self.key = key

    def request(self, method: str, path: str, *, body: bytes | None = None, headers: Mapping[str, str] | None = None,
                ok: Iterable[int] = (200,)) -> tuple[int, Any]:  # fmt: skip
        req = urllib.request.Request(self.base + path, data=body, method=method)
        req.add_header("authorization", f"Bearer {self.key}")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        for attempt in range(6):
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    raw = resp.read()
                    return resp.status, json.loads(raw) if raw else None
            except urllib.error.HTTPError as exc:
                raw = exc.read()
                if exc.code == 429:
                    time.sleep(float(exc.headers.get("retry-after", "2")))
                    continue
                if exc.code in set(ok):
                    return exc.code, json.loads(raw) if raw else None
                raise RuntimeError(f"{method} {path} -> {exc.code}: {raw[:300]!r}") from None
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                time.sleep(2 * (attempt + 1))
        raise RuntimeError(f"{method} {path}: gave up after retries")

    def get(self, path: str, **params: Any) -> Any:
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        return self.request("GET", path + (f"?{query}" if query else ""))[1]

    def upload(self, path: Path) -> dict[str, Any]:
        boundary = f"----eval{secrets.token_hex(8)}"
        body = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"files\"; filename=\"{path.name}\"\r\n"
            f"Content-Type: application/octet-stream\r\n\r\n"
        ).encode() + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
        _, outcomes = self.request(
            "POST", "/api/v1/documents", body=body, ok=(202, 400, 409, 413, 422),
            headers={"content-type": f"multipart/form-data; boundary={boundary}",
                     "idempotency-key": str(uuid.uuid4())},
        )  # fmt: skip
        if isinstance(outcomes, list) and outcomes:
            return dict(outcomes[0])
        return {"status": "rejected", "error": outcomes}  # whole-request rejection (e.g. 413)


# -------------------------------------------------------------- environment


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True).stdout.strip()


def quota_remaining() -> tuple[int | None, str]:
    out = compose("exec", "-T", "api", "python", "-m", "app.cli", "llm-check", check=False)
    for line in out.splitlines():
        if line.startswith("free models"):
            numbers = re.findall(r"\b\d+\b", line)
            if len(numbers) >= 3:
                return int(numbers[2]), line.strip()
    return None, out.strip()[-300:]


SETTINGS_DUMP: Final = """
import json
from app.config import ExtractionSettings, LLMSettings, PipelineSettings, ValidationSettings, SearchSettings
def safe(model):
    data = model.model_dump(mode="json")
    return {k: v for k, v in data.items() if not any(s in k.lower() for s in ("key", "secret", "password", "token"))}
out = {}
for cls in (LLMSettings, ExtractionSettings, PipelineSettings, ValidationSettings, SearchSettings):
    try:
        out[cls.__name__] = safe(cls())
    except Exception as exc:
        out[cls.__name__] = {"error": type(exc).__name__}
print(json.dumps(out, default=str))
"""


def environment(entries: list[ManifestEntry], args: argparse.Namespace) -> dict[str, Any]:
    info: dict[str, Any] = {
        "git_sha": git("rev-parse", "HEAD"),
        "git_dirty": bool(git("status", "--porcelain")),
        "started_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "host": {"platform": platform.platform(), "python": platform.python_version(), "cpus": os.cpu_count()},
        "split": args.split,
        "files": len(entries),
        "no_cache": args.no_cache,
    }
    docker = subprocess.run(["docker", "info", "--format", "{{.NCPU}} {{.MemTotal}}"], capture_output=True, text=True)
    if docker.returncode == 0 and docker.stdout.split():
        ncpu, mem = docker.stdout.split()[:2]
        info["docker_vm"] = {"cpus": int(ncpu), "mem_gb": round(int(mem) / 2**30, 2)}
    info["worker"] = {
        "tesseract": compose("exec", "-T", "worker", "tesseract", "--version", check=False).splitlines()[:1],
        "celery_cmd": compose("ps", "--format", "{{.Service}}|{{.Command}}", check=False).strip().splitlines(),
    }
    try:
        info["settings"] = json.loads(compose("exec", "-T", "api", "python", "-c", SETTINGS_DUMP, check=False) or "{}")
    except json.JSONDecodeError:
        info["settings"] = {"error": "could not read settings"}
    return info


# ------------------------------------------------------------------ collect

DB_QUERY: Final = """
select json_build_object(
  'extractions', coalesce((select json_agg(json_build_object(
      'id', e.id, 'invoice_index', e.invoice_index, 'version', e.version, 'is_current', e.is_current,
      'field_meta', e.field_meta, 'model_name', e.model_name, 'prompt_version', e.prompt_version,
      'token_usage', e.token_usage, 'latency_ms', e.latency_ms, 'overall_confidence', e.overall_confidence,
      'document_type', e.document_type, 'review_status', e.review_status) order by e.invoice_index, e.version)
    from extractions e where e.document_id = '{doc}'), '[]'::json),
  'jobs', coalesce((select json_agg(json_build_object(
      'id', j.id, 'status', j.status, 'attempts', j.attempts, 'error_code', j.error_code,
      'created_at', j.created_at, 'started_at', j.started_at, 'finished_at', j.finished_at) order by j.created_at)
    from processing_jobs j where j.document_id = '{doc}'), '[]'::json),
  'units', coalesce((select json_agg(json_build_object(
      'unit_index', u.unit_index, 'method', u.extraction_method, 'ocr_confidence', u.ocr_confidence) order by u.unit_index)
    from document_units u where u.document_id = '{doc}'), '[]'::json),
  'chunks', (select json_build_object('total', count(*), 'embedded', count(c.embedding))
    from chunks c where c.document_id = '{doc}')
)
"""


def collect(api: Api, doc_id: str) -> dict[str, Any]:
    detail = api.get(f"/api/v1/documents/{doc_id}")
    events, cursor = [], None
    while True:
        page = api.get(f"/api/v1/documents/{doc_id}/events", limit=200, cursor=cursor)
        events += page["items"]
        cursor = page.get("next_cursor")
        if not cursor:
            break
    status, results = api.request("GET", f"/api/v1/documents/{doc_id}/results", ok=(200, 404))
    uuid.UUID(doc_id)  # guards the SQL interpolation below
    db = sql_json(DB_QUERY.format(doc=doc_id))
    return {"detail": detail, "events": events, "results": results if status == 200 else None, "db": db}


def wait_terminal(api: Api, doc_ids: dict[str, str], timeout_s: float) -> dict[str, dict[str, Any]]:
    """Poll until every document is terminal or the timeout passes. Returns final details."""
    pending = dict(doc_ids)
    done: dict[str, dict[str, Any]] = {}
    deadline = time.monotonic() + timeout_s
    while pending and time.monotonic() < deadline:
        for file, doc_id in list(pending.items()):
            detail = api.get(f"/api/v1/documents/{doc_id}")
            if detail["status"] in TERMINAL:
                done[file] = {"detail": detail, "seen_terminal_at": dt.datetime.now(dt.UTC).isoformat()}
                pending.pop(file)
        if pending:
            print(f"  waiting: {len(pending)} not terminal ({', '.join(sorted(pending)[:3])}...)", flush=True)
            time.sleep(POLL_S * 5)
    for file in pending:
        done[file] = {"detail": None, "timed_out": True}
    return done


# --------------------------------------------------------------------- run


def soft_delete_previous(api: Api, names: set[str]) -> int:
    """Remove earlier uploads of these files so cross-document checks only see this run."""
    deleted, cursor = 0, None
    while True:
        page = api.get("/api/v1/documents", limit=100, cursor=cursor)
        for doc in page["items"]:
            if doc["filename"] in names:
                api.request("DELETE", f"/api/v1/documents/{doc['id']}", ok=(204, 404))
                deleted += 1
        cursor = page.get("next_cursor")
        if not cursor:
            return deleted


def flush_llm_cache() -> int:
    keys = compose("exec", "-T", "redis", "redis-cli", "--scan", "--pattern", "llm:cache:*").split()
    for i in range(0, len(keys), 200):
        compose("exec", "-T", "redis", "redis-cli", "del", *keys[i : i + 200])
    return len(keys)


def select(entries: list[ManifestEntry], args: argparse.Namespace) -> list[ManifestEntry]:
    chosen = [e for e in entries if e.split == args.split]
    if args.category:
        chosen = [e for e in chosen if e.category == args.category]
    if args.limit:
        chosen = chosen[: args.limit]
    return chosen


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]


def load_done(run_dir: Path) -> set[str]:
    """Files whose prediction was written (a resume skips them)."""
    return {row["file"] for row in read_jsonl(run_dir / "predictions.jsonl")}


def load_uploads(run_dir: Path) -> dict[str, dict[str, Any]]:
    """Files already uploaded in this run (a resume polls them instead of uploading again)."""
    return {row["file"]: row for row in read_jsonl(run_dir / "uploads.jsonl")}


def estimate(entries: Iterable[ManifestEntry]) -> dict[str, int]:
    entries = list(entries)
    llm = sum(e.llm_requests for e in entries)
    return {"extraction_requests": llm, "with_retry_margin": int(llm * RETRY_FACTOR + 0.999)}


def run(args: argparse.Namespace) -> int:
    env = read_env()
    key = os.environ.get("EVAL_API_KEY") or env.get("EVAL_API_KEY")
    if not key:
        print("EVAL_API_KEY is not set: run `python -m eval.run_eval --create-key` first", file=sys.stderr)
        return 2
    api = Api(args.api, key)
    entries = load_manifest()

    if args.resume:
        run_dir = Path(args.resume)
        meta = json.loads((run_dir / "environment.json").read_text("utf-8"))
        args.split = meta["split"]
        args.no_cache = meta["no_cache"]
        selected = [e for e in entries if e.file in set(meta["selected_files"])]
        meta.setdefault("resumes", []).append(dt.datetime.now(dt.UTC).isoformat(timespec="seconds"))
    else:
        selected = select(entries, args)
        stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
        run_dir = RESULTS / f"{stamp}_{git('rev-parse', '--short', 'HEAD')}_{args.split}"
        meta = environment(selected, args)
        meta["selected_files"] = [e.file for e in selected]
        meta["resumes"] = []
    done = load_done(run_dir)
    uploaded = load_uploads(run_dir)
    todo = [e for e in selected if e.file not in done]
    need = estimate(e for e in todo if e.file not in uploaded)
    remaining, quota_line = quota_remaining()
    meta.setdefault("quota_checks", []).append(
        {"at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"), "remaining": remaining, "need": need}
    )
    print(f"{len(todo)} of {len(selected)} files to run; estimated LLM requests {need}; quota: {quota_line}")
    if remaining is not None and remaining < need["with_retry_margin"] and not args.allow_deferral:
        print(
            f"Not enough quota: {remaining} requests left, about {need['with_retry_margin']} needed.\n"
            "Wait for the UTC reset, use --limit, or pass --allow-deferral (documents wait for the reset;\n"
            "the run can then be continued with --resume).",
            file=sys.stderr,
        )
        return 3
    if args.dry_run:
        return 0
    run_dir.mkdir(parents=True, exist_ok=True)
    if not args.resume:
        meta["deleted_previous_uploads"] = soft_delete_previous(api, {e.file for e in entries})
        meta["llm_cache_keys_flushed"] = flush_llm_cache() if args.no_cache else 0
    (run_dir / "environment.json").write_text(json.dumps(meta, indent=2, default=str), encoding="utf-8")

    # Upload in manifest order (duplicate pairs rely on "later" = later created_at).
    # Each upload is checkpointed at once, so a resume never uploads a file twice.
    uploads = {f: u for f, u in uploaded.items() if f not in done}
    with (run_dir / "uploads.jsonl").open("a", encoding="utf-8") as log:
        for entry in todo:
            if entry.file in uploads:
                continue
            started = time.time()
            outcome = api.upload(DATASET_DIR / "files" / entry.file)
            row = {"file": entry.file, "outcome": outcome, "uploaded_at": started, "upload_s": time.time() - started}
            uploads[entry.file] = row
            log.write(json.dumps(row, default=str) + "
")
            log.flush()
            print(f"  uploaded {entry.file}: {outcome.get('status')}", flush=True)
    doc_ids = {f: u["outcome"]["document_id"] for f, u in uploads.items() if u["outcome"].get("document_id")}
    finals = wait_terminal(api, doc_ids, args.timeout)
    # Counterparts are re-validated when their pair arrives: collect after everything settled.
    with (run_dir / "predictions.jsonl").open("a", encoding="utf-8") as out:
        for entry in todo:
            record: dict[str, Any] = {**uploads[entry.file]}
            doc_id = doc_ids.get(entry.file)
            if doc_id:
                record["document_id"] = doc_id
                record["timed_out"] = finals[entry.file].get("timed_out", False)
                record["seen_terminal_at"] = finals[entry.file].get("seen_terminal_at")
                record.update(collect(api, doc_id))
            out.write(json.dumps(record, default=str) + "\n")
    meta["finished_at"] = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    meta["quota_after"] = quota_remaining()[0]
    (run_dir / "environment.json").write_text(json.dumps(meta, indent=2, default=str), encoding="utf-8")
    if not args.skip_search:
        from eval.search_eval import run_search_eval

        run_search_eval(api, run_dir, [e for e in selected])
    print(f"done: {run_dir}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--split", choices=("dev", "test"), default="dev")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--category")
    parser.add_argument("--resume", help="continue an interrupted run directory")
    parser.add_argument("--no-cache", action="store_true", help="flush the LLM response cache first (official runs)")
    parser.add_argument("--allow-deferral", action="store_true", help="start even if the quota will run out")
    parser.add_argument("--dry-run", action="store_true", help="estimate and check quota only")
    parser.add_argument("--skip-search", action="store_true")
    parser.add_argument("--timeout", type=float, default=DOC_TIMEOUT_S)
    parser.add_argument("--api", default=os.environ.get("EVAL_API_URL", "http://localhost:8000"))
    parser.add_argument("--create-key", action="store_true")
    args = parser.parse_args(argv)
    if args.create_key:
        create_key()
        return 0
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
