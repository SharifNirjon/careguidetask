"""End-to-end walkthrough against the running stack (doubles as the demo-video script).

    docker compose up -d --build
    python scripts/e2e_api.py --create-key            # or: E2E_API_KEY=cg_... python scripts/e2e_api.py

create key -> upload 4 mixed files -> poll until done -> fetch results -> correct
a field -> resolve an issue -> approve -> reprocess a failed document -> search.

Inputs are generated synthetic invoices (tests/fixtures/factory.py): free LLM
endpoints may log traffic, so real documents must never be used here.
Standard library only (urllib), so it runs anywhere Python does.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FINAL_STATUSES = {"completed", "needs_review", "failed"}
POLL_INTERVAL_S = 3.0
HTTP_TIMEOUT_S = 60
CONNECTION_RETRIES = 4


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: Any


class Client:
    def __init__(self, base: str, key: str) -> None:
        self.base = base.rstrip("/")
        self.key = key
        self.request_id = f"e2e-{uuid.uuid4().hex[:12]}"

    def call(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> Response:
        all_headers = {
            "Authorization": f"Bearer {self.key}",
            "X-Request-ID": self.request_id,
            **(headers or {}),
        }
        if json_body is not None:
            data = json.dumps(json_body).encode()
            all_headers["Content-Type"] = "application/json"
        # Only safe-to-repeat requests are retried: GETs, and uploads carrying an Idempotency-Key.
        retryable = method == "GET" or "Idempotency-Key" in all_headers
        attempts = CONNECTION_RETRIES if retryable else 1
        for attempt in range(1, attempts + 1):
            request = urllib.request.Request(
                f"{self.base}{path}", data=data, method=method, headers=all_headers
            )
            try:
                with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_S) as raw:
                    return Response(raw.status, _lower(raw.headers), _decode(raw.read()))
            except urllib.error.HTTPError as err:
                return Response(err.code, _lower(err.headers), _decode(err.read()))
            except (ConnectionError, TimeoutError) as err:
                if attempt == attempts:
                    raise
                print(f"  (connection problem: {type(err).__name__}; retrying)", flush=True)
                time.sleep(POLL_INTERVAL_S * attempt)
        raise AssertionError("unreachable")

    def upload(self, files: dict[str, bytes], idempotency_key: str) -> Response:
        boundary = uuid.uuid4().hex
        parts = []
        for name, content in files.items():
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="{name}"\r\n'
                "Content-Type: application/octet-stream\r\n\r\n".encode()
                + content
                + b"\r\n"
            )
        body = b"".join(parts) + f"--{boundary}--\r\n".encode()
        return self.call(
            "POST",
            "/api/v1/documents",
            data=body,
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Idempotency-Key": idempotency_key,
            },
        )


def _lower(headers: Any) -> dict[str, str]:
    """HTTP header names are case-insensitive; normalize so lookups can't miss."""
    return {k.lower(): v for k, v in headers.items()}


def _decode(raw: bytes) -> Any:
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return raw[:200]


def step(title: str) -> None:
    print(f"\n=== {title}", flush=True)


def show(label: str, value: Any) -> None:
    print(f"  {label:<22} {value}")


def create_key() -> str:
    """Runs the CLI inside the api container; the key is printed once and kept only in memory."""
    out = subprocess.run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "api",
            "python",
            "-m",
            "app.cli",
            "api-key",
            "create",
            "--name",
            "e2e-demo",
            "--role",
            "admin",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return next(line.split(":", 1)[1].strip() for line in out.splitlines() if line.startswith("key:"))


def synthetic_files() -> dict[str, bytes]:
    from tests.fixtures import factory

    return {
        "invoice_INV-2024-001.pdf": factory.digital_pdf([factory.invoice_lines("INV-2024-001")]),
        "scanned_receipt.png": factory.blurry_png(),
        "ledger.xlsx": factory.invoice_xlsx(),
        "damaged.pdf": factory.truncated_pdf(),  # passes intake, fails at parse: reprocessed below
    }


def wait_until_done(client: Client, ids: dict[str, str], timeout_s: float) -> dict[str, dict[str, Any]]:
    deadline, etags = time.monotonic() + timeout_s, {}
    latest: dict[str, dict[str, Any]] = {}
    while time.monotonic() < deadline:
        for name, document_id in ids.items():
            if latest.get(name, {}).get("status") in FINAL_STATUSES:
                continue
            headers = {"If-None-Match": etags[name]} if name in etags else {}
            response = client.call("GET", f"/api/v1/documents/{document_id}", headers=headers)
            if response.status == 304:
                continue
            etags[name] = response.headers.get("etag", "")
            latest[name] = response.body
            progress = response.body["progress"]
            print(
                f"  {name:<26} {response.body['status']:<13} stage={progress['stage']} {progress['percent']}%"
            )
        if all(d.get("status") in FINAL_STATUSES for d in latest.values()) and len(latest) == len(ids):
            return latest
        time.sleep(POLL_INTERVAL_S)
    raise SystemExit(f"timed out after {timeout_s:.0f}s waiting for processing")


Extraction = dict[str, Any]


def connect(base_url: str, create: bool) -> Client:
    step("1. API key")
    key = create_key() if create else os.environ.get("E2E_API_KEY", "")
    if not key:
        raise SystemExit("pass --create-key or set E2E_API_KEY")
    client = Client(base_url, key)
    show("prefix", "_".join(key.split("_")[:2]))  # never print the secret part
    show("request id", client.request_id)
    ready = client.call("GET", "/health/ready")
    show("health/ready", f"{ready.status} {ready.body['components'] if ready.body else ''}")
    return client


def upload_files(client: Client) -> dict[str, str]:
    step("2. Upload 4 mixed files (with Idempotency-Key)")
    files = synthetic_files()
    idem = f"e2e-{uuid.uuid4().hex}"
    upload = client.upload(files, idem)
    for outcome in upload.body:
        show(outcome["filename"], f"{outcome['status']} {outcome.get('document_id') or outcome.get('error')}")
    show("replay identical", client.upload(files, idem).body == upload.body)
    return {o["filename"]: o["document_id"] for o in upload.body if o["status"] == "queued"}


def show_results(client: Client, document_id: str) -> Extraction:
    step("4. Results for the PDF invoice")
    results = client.call("GET", f"/api/v1/documents/{document_id}/results").body
    if not results["extractions"]:
        raise SystemExit("no extraction for the PDF (is the worker running with OPENROUTER_API_KEY?)")
    extraction: Extraction = results["extractions"][0]
    for name in ("invoice_number", "invoice_date", "supplier_name", "total", "currency"):
        field = extraction["fields"][name]
        show(name, f"{field['value']!s:<24} conf={field['confidence']} source={field['source']}")
    show("issues", [(i["rule_code"], i["severity"]) for i in extraction["issues"]])
    show("review_status", extraction["review_status"])
    return extraction


def correct(client: Client, extraction: Extraction) -> Extraction:
    step("5. Correct a field (If-Match: lock_version)")
    url = f"/api/v1/extractions/{extraction['id']}"
    patch = client.call(
        "PATCH",
        url,
        headers={"If-Match": str(extraction["lock_version"])},
        json_body={"fields": {"customer_name": "Northwind Clinic (verified)"}, "reason": "e2e walkthrough"},
    )
    show("status", patch.status)
    updated: Extraction = patch.body if patch.status == 200 else extraction
    show("lock_version", updated["lock_version"])
    stale = client.call("PATCH", url, headers={"If-Match": "1"}, json_body={"fields": {"customer_name": "x"}})
    show("stale If-Match", f"{stale.status} {stale.body.get('code') if isinstance(stale.body, dict) else ''}")
    return updated


def review(client: Client, extraction: Extraction) -> None:
    step("6. Resolve open issues")
    for issue in [i for i in extraction["issues"] if not i["resolved"]]:
        resolved = client.call(
            "POST",
            f"/api/v1/issues/{issue['id']}/resolve",
            json_body={"note": "Checked against the paper original"},
        )
        show(issue["rule_code"], resolved.status)
    step("7. Approve")
    approve = client.call(
        "POST", f"/api/v1/extractions/{extraction['id']}/approve", json_body={"note": "e2e"}
    )
    show("status", f"{approve.status} {approve.body.get('review_status') or approve.body.get('code')}")


def reprocess_failed(client: Client, ids: dict[str, str], docs: dict[str, dict[str, Any]]) -> None:
    step("8. Reprocess the failed document")
    failed = [n for n, d in docs.items() if d["status"] == "failed"]
    for name in failed:
        show(f"{name} error", docs[name]["error"])
        job = client.call(
            "POST", f"/api/v1/documents/{ids[name]}/reprocess", json_body={"from_stage": "parse"}
        )
        show("reprocess", f"{job.status} job={job.body.get('id')} type={job.body.get('job_type')}")
    if not failed:
        show("reprocess", "no failed document this run")


def search(client: Client, extraction: Extraction) -> None:
    step("9. Search")
    number = extraction["fields"]["invoice_number"]["value"] or "INV"
    hits = client.call("GET", f"/api/v1/search?q={urllib.parse.quote(number)}").body["items"]
    for hit in hits[:3]:
        show(f"score {hit['score']}", hit["snippet"])
    show("semantic (Step 7)", client.call("GET", "/api/v1/search?q=late%20fees&mode=semantic").status)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=os.environ.get("E2E_BASE_URL", "http://localhost:8000"))
    parser.add_argument("--create-key", action="store_true", help="create an admin key via the api container")
    parser.add_argument("--timeout", type=float, default=900, help="seconds to wait for processing")
    args = parser.parse_args()

    client = connect(args.base_url, args.create_key)
    ids = upload_files(client)
    step("3. Poll until processed (ETag / 304 keeps polling cheap)")
    docs = wait_until_done(client, ids, args.timeout)
    extraction = correct(client, show_results(client, ids["invoice_INV-2024-001.pdf"]))
    review(client, extraction)
    reprocess_failed(client, ids, docs)
    search(client, extraction)
    print()
    print(f"Done. Every request carried X-Request-ID {client.request_id} (grep the api/worker logs for it).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
