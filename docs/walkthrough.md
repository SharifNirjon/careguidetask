# Walkthrough recording: script

A 6–8 minute screen recording of the working application. Record at 1440×900 or
1920×1080 with the browser zoomed to 100 %. Use only the synthetic files listed below
(free LLM endpoints may log traffic).

**Before recording**

```bash
docker compose up -d                      # wait until `docker compose ps` shows api healthy
python scripts/make_e2e_fixtures.py       # frontend/e2e/fixtures/*
python -m eval.build_dataset              # eval/dataset/files/* (more sample files)
```

Open http://localhost:3000 in a fresh browser window. Have these files ready:
`frontend/e2e/fixtures/invoice-mismatch.pdf`, `scanned-receipt.png`, `ledger.xlsx`,
`corrupt.pdf`, and from `eval/dataset/files/`: `ev-multi-1.pdf`,
`ev-sheet-register-offset.xlsx`, `ev-csv-06-semicolon-cp1252.csv`.

## Scenes

| # | Time | Show | Say |
|---|---|---|---|
| 1 | 0:00 | The README architecture diagram | "Uploads go through a Next.js server that holds the API key; FastAPI stores and queues; Celery workers parse, OCR, extract with an LLM or with rules, validate and index." |
| 2 | 0:40 | Visit `/documents` → redirected to the login page; sign in | "Every page needs a session; the browser never sees the API key." |
| 3 | 1:00 | Upload: drag the 4 fixture files plus the multi-invoice PDF, the XLSX and the CSV | "One batch, one idempotency key; each file gets its own outcome." Point at **corrupt.pdf** being rejected with a reason. |
| 4 | 1:40 | Upload dialog: statuses change live; then the documents list | "Spreadsheets finish in seconds with zero LLM calls; PDFs and scans take longer." Show the filters and the **Needs review** filter. |
| 5 | 2:30 | Open **invoice-mismatch.pdf** | Page viewer with zoom and thumbnails; each field shows confidence, source and the evidence text; **Show on page**. |
| 6 | 3:10 | The **TOTAL_MISMATCH** issue | "Subtotal 225 + VAT 45 is 270, but the page says 300. Validation never changes the value; it suggests one." Press **Apply suggestion**, then **Ctrl+S**: the issue disappears. |
| 7 | 3:50 | Press **?** (shortcuts), then **A** to approve | "The whole review works from the keyboard." Show the **Versions** and **Timeline** tabs (stage durations). |
| 8 | 4:30 | Open **ev-multi-1.pdf** | Invoice switcher: 5 invoices found in one file, each with its page range. |
| 9 | 5:00 | Open the XLSX | Sheet view with the cells highlighted; "header on row 4 and merged cells handled". |
| 10 | 5:30 | Open **scanned-receipt.png** | OCR method and confidence; low-confidence fields flagged. |
| 11 | 6:00 | **Search**: keyword `E2E-2026-0042`, then hybrid "gloves" | Highlighted snippets; if embeddings are not ready, the degraded banner explains the keyword fallback. |
| 12 | 6:40 | **Reconciliation**: pick the PDF and the ledger, run | Matched / mismatched report. |
| 13 | 7:10 | Sign out; `/documents` redirects to login again | "Sessions end on the server." Close with the test and evaluation pages in `docs/`. |

## Recording options

- **Manual** (recommended, with voice-over): OBS Studio or the Windows Game Bar
  (Win+Alt+R).
- **Automated, no voice**: the Playwright e2e can record video. Run
  `npx playwright test --project=chromium -c playwright.config.ts` with
  `use: { video: "on" }` set in `frontend/playwright.config.ts`; the `.webm` files are
  written to `frontend/test-results/`.

Keep the final video out of git (large binary). Upload it and put the link in the
README's "Walkthrough" line.
