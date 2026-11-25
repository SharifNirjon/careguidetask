"""Generate docs/test-results/summary.md from REAL test output. No number is typed by hand.

    python scripts/test_report.py                         # runs the suites, then writes the report
    python scripts/test_report.py --backend-log FILE      # reuse a saved full-suite pytest log

Backend: `pytest --cov=backend/app --cov-branch --cov-report=term` (needs PostgreSQL and
Redis from docker compose). Its terminal output is saved as backend-pytest.log; the
summary line and the coverage table are parsed from it.
Eval metrics tests (pure) and the frontend unit tests (Vitest) are run and parsed from
JUnit XML. The Playwright e2e result is read from its JUnit file if one exists.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import platform
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "test-results"
PY = sys.executable
NPM = shutil.which("npm") or shutil.which("npm.cmd") or "npm"


def run(cmd: list[str], cwd: Path, log: Path | None = None) -> int:
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if log:
        log.write_text(proc.stdout + proc.stderr, encoding="utf-8")
    return proc.returncode


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True).stdout.strip()


def parse_pytest_log(text: str) -> dict[str, Any]:
    text = text.lstrip("﻿")
    summary = re.findall(r"^=*\s*(\d+ passed.*?) in ([\d.]+)s", text, re.M) or re.findall(
        r"^(\d+ passed.*?) in ([\d.]+)s", text, re.M
    )
    counts: dict[str, int] = {}
    duration = None
    if summary:
        line, secs = summary[-1]
        duration = float(secs)
        for n, word in re.findall(r"(\d+) (passed|failed|skipped|errors?|xfailed|xpassed|warnings?)", line):
            counts[word.rstrip("s") if word.startswith(("error", "warning")) else word] = int(n)
    # Per file, coverage.py prints Stmts Miss Branch BrPart Cover%. BrPart counts PARTIAL
    # branches, not missing ones, so the percentage can't be rebuilt from the counts:
    # weight each file's reported Cover% by its size (exact up to whole-percent rounding).
    weighted: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
    for m in re.finditer(r"^(\S*app[\\/]\S+\.py)\s+(\d+)\s+\d+\s+(\d+)\s+\d+\s+(\d+)%", text, re.M):
        path = m.group(1).replace("\\", "/")
        size, pct = int(m.group(2)) + int(m.group(3)), int(m.group(4))
        parts = path.split("app/", 1)[1].split("/")
        keys = [parts[0] if len(parts) > 1 else "(top-level modules)"]
        if path.endswith("extraction/normalize.py"):
            keys.append("extraction/normalize.py")
        for key in keys:
            weighted[key][0] += pct * size
            weighted[key][1] += size
    cov = {k: num / den for k, (num, den) in weighted.items() if den}
    t = re.search(r"^TOTAL\s+\d+\s+\d+\s+\d+\s+\d+\s+(\d+)%", text, re.M)
    total = float(t.group(1)) if t else None
    slow = re.findall(r"^([\d.]+)s (call|setup|teardown)\s+(\S+)", text, re.M)[:5]
    return {"counts": counts, "duration_s": duration, "coverage_total": total, "coverage": cov, "slowest": slow}


def parse_junit(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    out = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0, "time": 0.0}
    for s in suites:
        for key in ("tests", "failures", "errors", "skipped"):
            out[key] += int(s.get(key, 0))
        out["time"] += float(s.get("time", 0) or 0)
    out["passed"] = out["tests"] - out["failures"] - out["errors"] - out["skipped"]
    return out


GATES = {"(overall)": 85, "validation": 95, "extraction/normalize.py": 95, "extraction": 85, "pipeline": 85, "api": 85}


def render(backend: dict[str, Any], log_note: str, suites: dict[str, dict[str, Any] | None]) -> str:
    now = dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M UTC")
    dirty = " (plus uncommitted changes)" if git("status", "--porcelain") else ""
    c = backend["counts"]
    lines = [
        "# Test results",
        "",
        f"Generated {now} by `scripts/test_report.py` from real test output. Do not edit by hand.",
        "",
        f"- Commit: `{git('rev-parse', '--short', 'HEAD')}`{dirty}",
        f"- Host: {platform.platform()}, Python {platform.python_version()}",
        f"- Backend log: {log_note}",
        "",
        "## Suites",
        "",
        "| Suite | Passed | Failed | Skipped | Duration |",
        "|---|---:|---:|---:|---:|",
        f"| Backend (unit + integration, PostgreSQL + Redis) | {c.get('passed', 0)} | {c.get('failed', 0) + c.get('error', 0)} "
        f"| {c.get('skipped', 0)} | {backend['duration_s'] or 0:.0f} s |",
    ]
    for name, res in suites.items():
        if res is None:
            lines.append(f"| {name} | not run | | | |")
        else:
            lines.append(
                f"| {name} | {res['passed']} | {res['failures'] + res['errors']} | {res['skipped']} | {res['time']:.0f} s |"
            )
    lines += [
        "",
        "Skipped backend tests: OCR tests need the Tesseract binary (they run in the worker image),",
        "and live LLM tests run only with `RUN_LIVE_LLM=1` (they spend real quota).",
        "",
        "## Backend branch coverage",
        "",
        "Overall is pytest-cov's TOTAL; packages are size-weighted from the per-file percentages",
        "(coverage.py rounds those to whole percent, so a package value can be off by up to 0.5).",
        "",
        "| Package | Coverage | Gate | Met |",
        "|---|---:|---:|:---:|",
    ]
    rows = [("(overall)", backend["coverage_total"])] + sorted(backend["coverage"].items())
    for name, value in rows:
        if value is None:
            continue
        gate = GATES.get(name)
        met = "" if gate is None else ("yes" if value >= gate else "**no**")
        lines.append(f"| {name} | {value:.1f} % | {'' if gate is None else f'{gate} %'} | {met} |")
    if backend["slowest"]:
        lines += ["", "## Slowest backend tests", "", "| Time | Phase | Test |", "|---:|---|---|"]
        lines += [f"| {t} s | {phase} | `{name}` |" for t, phase, name in backend["slowest"]]
    lines += [
        "",
        "## Files",
        "",
        "- `backend-pytest.log`: the full backend run (pytest + coverage output).",
        "- `eval-junit.xml`, `frontend-junit.xml`: JUnit XML of the other suites.",
        "",
        "What each test covers: [../testing/matrix.md](../testing/matrix.md).",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend-log", type=Path, help="reuse a saved full backend pytest log")
    parser.add_argument("--skip-frontend", action="store_true")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    log = OUT / "backend-pytest.log"
    if args.backend_log:
        log.write_text(args.backend_log.read_text("utf-8", errors="replace").lstrip("﻿"), encoding="utf-8")
        stamp = dt.datetime.fromtimestamp(args.backend_log.stat().st_mtime, dt.UTC).strftime("%Y-%m-%d %H:%M UTC")
        note = f"saved run finished {stamp} (`--backend-log`)"
    else:
        run([PY, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--cov=backend/app", "--cov-branch",
             "--cov-report=term", "--durations=5"], ROOT, log)  # fmt: skip
        note = "run by this script"
    backend = parse_pytest_log(log.read_text("utf-8"))
    run([PY, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/eval", f"--junitxml={OUT / 'eval-junit.xml'}"],
        ROOT)  # fmt: skip
    suites: dict[str, dict[str, Any] | None] = {"Evaluation metrics (pure)": parse_junit(OUT / "eval-junit.xml")}
    if not args.skip_frontend:
        run([NPM, "test", "--", "--reporter=junit", f"--outputFile={OUT / 'frontend-junit.xml'}"], ROOT / "frontend")
    suites["Frontend unit (Vitest)"] = parse_junit(OUT / "frontend-junit.xml")
    suites["Frontend e2e (Playwright)"] = parse_junit(OUT / "e2e-junit.xml")
    (OUT / "summary.md").write_text(render(backend, note, suites), encoding="utf-8", newline="\n")
    print(f"wrote {OUT / 'summary.md'}")
    return 0


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUTF8", "1")
    raise SystemExit(main())
