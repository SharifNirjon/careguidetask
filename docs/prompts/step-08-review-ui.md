<!-- Prompt given to Claude Code for this step, verbatim (sent 2026-11-25 14:56 UTC). -->

# Role
You are a senior frontend engineer building the review UI for a production
document-intelligence system. Reviewers use it all day, so it must be fast, clear about
what needs attention, keyboard-friendly, accessible and secure. Write clean, typed,
maintainable code. If anything here is wrong or risky, say so before coding.

# Context
- The backend (Steps 2–7) is done: REST API at /api/v1, OpenAPI at docs/openapi.json.
  Read docs/api.md and docs/search.md first. Follow CLAUDE.md, adapted to TypeScript.
- API behaviors the UI must respect:
  - money values are decimal STRINGS
  - errors are problem+json and include a request_id
  - ETag / If-None-Match on document detail and results
  - PATCH corrections require If-Match (the lock_version)
  - uploads return one outcome per file
  - search returns snippet_segments and may return degraded: true
  - every semantic/hybrid query spends LLM quota
- The browser must NEVER see the API key.

# Stack
Next.js (latest stable, App Router) + TypeScript strict, Tailwind CSS, shadcn/ui (Radix
primitives), TanStack Query, react-hook-form + zod, openapi-typescript + openapi-fetch
(generated types), react-dropzone, Vitest + React Testing Library + MSW, Playwright +
@axe-core/playwright. Add nothing else without a reason.

# 1. Architecture (frontend/)
src/
  app/                  # routes (thin pages)
  features/{upload,documents,review,search,reconciliation,auth}/
      components/  hooks/  api.ts  schemas.ts
  components/ui/        # shared primitives (shadcn)
  lib/api/              # generated types, typed client, error mapping
  lib/format/           # money, dates, file sizes
- Pages are thin. Each feature owns its components, hooks and queries.
- Components never call fetch directly; they use feature hooks (useDocument,
  useCorrectExtraction, …).
- Server Components for shells and static layout; Client Components only where the UI is
  interactive.
- Server state lives in TanStack Query (a query-key factory per feature, targeted
  invalidation after mutations). Filter and UI state live in URL search params
  (shareable, back-button safe). No global store.

# 2. Auth & API access (backend-for-frontend)
- Minimal login: credentials from server env (DEMO_USERNAME, DEMO_PASSWORD_HASH). The
  session is an encrypted httpOnly, Secure, SameSite=Lax cookie (e.g. iron-session).
  Include logout. Middleware protects every route except /login.
- A route handler at /api/proxy/[...path] forwards requests to BACKEND_URL with the
  server-only API_KEY. It is NOT an open proxy:
  - allowlist of paths and methods
  - passes through If-Match, If-None-Match, Idempotency-Key and X-Request-ID
  - streams request and response bodies, so uploads up to the backend limit work without
    buffering in memory
- CSRF: mutating proxy requests require same-origin (Origin header check) plus the
  SameSite cookie.
- No NEXT_PUBLIC_* variables for secrets. Security headers in next.config (CSP, nosniff,
  frame-ancestors none, Referrer-Policy). No dangerouslySetInnerHTML anywhere.

# 3. Generated API client
- `npm run gen:api` generates types from ../docs/openapi.json. A check script fails if
  the generated types are stale. No hand-written API types.
- One error mapper turns problem+json into a typed AppError {code, title, detail,
  fieldErrors, requestId}, with user-facing messages for known codes (TYPE_MISMATCH,
  FILE_TOO_LARGE, PASSWORD_PROTECTED, LLM_QUOTA_DEFERRED, …). Show the request_id in
  error details.

# 4. Formatting
- Money: format decimal strings WITHOUT converting to float (Intl.NumberFormat with
  string input where supported, or a tiny decimal-string formatter), respecting each
  currency's minor units.
- Dates: the API sends ISO; display them in the user's locale. For invoice dates, always
  show the ISO value in a tooltip (avoids DD/MM vs MM/DD confusion).

# 5. Screens

Upload (dialog or page)
- Drag-and-drop multiple files. Client-side checks on type, size and count are hints
  only; the server decides.
- Per-file upload progress via XHR upload events (fetch has none). Then per-file
  processing progress by polling the document with ETag: 1 s → 5 s backoff, stop at
  terminal states, pause while the tab is hidden.
- One Idempotency-Key per batch, so retrying after a network error never duplicates.
- Each file shows its outcome: queued, duplicate (with a link to the original), or an
  error with a friendly message.

Documents list
- Columns: file, type, status, review status, supplier, invoice no., date, total, open
  issues by severity, uploaded at. Status and severity use an icon plus text, never color
  alone.
- Filters and sort synced to the URL; cursor pagination; auto-refresh while any visible
  row is processing; skeleton, empty and error states.
- A "Needs review" quick filter, and a "Start reviewing" button that opens the oldest
  document needing review.

Document detail / review (the core screen)
- Split view.
- Left, the document viewer:
  - page images from /units/{index}/image, with thumbnails, zoom and keyboard page
    navigation
  - a download link for the original file
  - spreadsheets render as the parsed table, with the referenced cell highlighted
- Right, the review panel:
  - Header: status, review status, confidence, processing time; buttons for Reprocess
    (stage select + confirm), Approve, and Reject (note required).
  - Fields form: each field shows its value input, a confidence badge, a grounded /
    ungrounded indicator, its source (llm | rule | manual), and the evidence snippet with
    a "Show on page" button that jumps to unit_index. Ungrounded values the model
    suggested appear as "Model suggested X — not found in document".
  - Line items: an editable table (add, edit, delete) with a live client-side arithmetic
    hint (UX only; the server validates).
  - Issues: grouped by severity, each with its message and expected vs actual values.
    "Apply suggestion" fills the field when expected_value exists (the user still saves).
    Resolve with a note, reopen. Clicking an issue focuses its field.
  - Tabs: Versions & corrections (the audit trail), Reconciliations (links to
    counterpart documents, with diffs), Processing timeline (events with durations).
- Saving:
  - react-hook-form + a zod schema that mirrors the backend formats; send only dirty
    fields, with If-Match set to lock_version.
  - 412 → a conflict dialog (reload, or keep my changes to re-apply).
  - 422 → inline field errors.
  - After saving, issues and status refresh.
- Approve stays disabled, with an explanation, while unresolved errors exist.
- Unsaved-changes guard on navigation.
- Keyboard shortcuts: Ctrl/Cmd+S save, J/K next/previous issue, A approve, N next
  document needing review, and a "?" help overlay.
- Multi-invoice documents get an invoice switcher (invoice 1..n, with page ranges).

Search
- A query input, a mode toggle (keyword | semantic | hybrid; default hybrid), and the
  same filters as the list.
- Keyword searches as you type (debounced 300 ms). Semantic and hybrid run on Enter or
  after 600 ms idle, and never for queries under 3 characters, because they spend quota.
- Results show headline fields, match type and score, with snippet_segments rendered as
  text spans (highlights wrapped in <mark>). Show a banner when degraded: true.
- Clicking a result opens the review screen at the matching page.

Reconciliation (only after everything else passes)
- Pick a left set (e.g. PDFs) and a right set (e.g. spreadsheets) from the documents
  list, run the check, and show the report: matched, mismatched (with field diffs),
  only-in-left, only-in-right, ambiguous.

# 6. UX quality
- Loading skeletons, empty states with a next action, error states with retry.
- Toasts for mutations. Optimistic updates only for resolving/reopening issues.
- The review screen targets desktop (≥ 1280 px); the list and search work on tablets.
- Accessibility: semantic HTML, labelled inputs, visible focus, full keyboard support,
  aria-live for progress and save results, AA contrast.
- Performance: lazy-load the viewer and page images, virtualize tables over 200 rows,
  keep the initial JS bundle lean, and report the build size.

# 7. Code quality
- TypeScript strict. No `any`, and no non-null assertions without a comment explaining
  why.
- ESLint (next + typescript-eslint strict) and Prettier. No unused code. Small,
  single-purpose components. Logic lives in hooks. Don't duplicate business rules beyond
  UX hints.
- Consistent naming; tests live next to their features.

# 8. Docker & config
- Multi-stage Dockerfile with `output: 'standalone'` and a non-root user. Add the
  frontend service to docker-compose.
- Server-only env: BACKEND_URL, API_KEY, SESSION_SECRET, DEMO_USERNAME,
  DEMO_PASSWORD_HASH, all documented in .env.example. Add a small script that generates
  the password hash.

# 9. Tests
- Vitest + RTL + MSW:
  - money and date formatters (table tests)
  - the error mapper
  - upload flow: progress, per-file errors, the Idempotency-Key reused on retry
  - fields form: PATCH sends only dirty fields, If-Match is sent, 412 opens the conflict
    dialog, 422 shows inline errors
  - Apply suggestion; Approve disabled while errors are open
  - snippet rendering escapes HTML
  - search debounce rules
- Proxy route tests: rejects non-allowlisted paths and methods, adds the API key
  server-side, rejects cross-origin mutations.
- Playwright e2e against docker compose: login → upload a PDF, a scanned image, an XLSX
  and a corrupt file → wait for processing → open a document needing review → correct
  the total → the TOTAL_MISMATCH issue disappears → approve → search finds the document
  → logout.
- Run axe on the list, review and search pages: no serious violations.
- A build test that greps the client bundle for the API key value and fails if found.
- Capture a screenshot of each main screen into docs/images/ (for the README and the
  video).

# Working style
1. Before coding: restate the plan in ≤10 bullets, list assumptions, flag disagreements.
2. Order: scaffold + tooling → auth + proxy → API client → list → upload → review →
   search → reconciliation → polish → tests → Docker.
3. Finish with: files created, commands, screenshots, bundle size, follow-ups.

# Acceptance criteria
- `npm run lint`, `npm run typecheck`, `npm test` and `npx playwright test` pass.
- `docker compose up` serves the UI on :3000, behind login.
- The API key never reaches the browser: the bundle check passes, and the network tab
  shows only /api/proxy calls.
- The full review flow works with the keyboard alone.
