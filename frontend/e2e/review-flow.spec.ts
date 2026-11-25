import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";

/**
 * The whole review flow against the running stack:
 * login -> upload 4 files -> wait for processing -> open the invoice needing
 * review -> correct the total (keyboard) -> TOTAL_MISMATCH disappears ->
 * approve -> search finds it -> logout. Axe runs on the list, review and search
 * pages; screenshots go to docs/images/ for the README and the video.
 */
const HERE = dirname(fileURLToPath(import.meta.url));
const FIXTURES = join(HERE, "fixtures");
const SHOTS = join(HERE, "..", "..", "docs", "images");
const INVOICE_NUMBER = "E2E-2026-0042";
const PROCESSING_TIMEOUT = 15 * 60_000;

async function expectNoSeriousA11yViolations(page: Page, name: string) {
  const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21aa"]).analyze();
  const serious = results.violations.filter((v) => v.impact === "serious" || v.impact === "critical");
  expect(
    serious.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(", ")}`),
    `axe violations on ${name}`,
  ).toEqual([]);
}

test("review flow", async ({ page }) => {
  const username = process.env.E2E_USERNAME ?? "demo";
  const password = process.env.E2E_PASSWORD;
  test.skip(!password, "set E2E_PASSWORD (see .env.example)");

  // ---- login (protected route redirects to /login?next=...)
  await page.goto("/documents");
  await expect(page).toHaveURL(/\/login/);
  await page.screenshot({ path: join(SHOTS, "ui-login.png") });
  await page.getByLabel("Username").fill(username);
  await page.getByLabel("Password").fill(password ?? "");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: "Documents" })).toBeVisible();

  // ---- upload 4 mixed files
  await page.getByRole("button", { name: "Upload" }).first().click();
  const dialog = page.getByRole("dialog", { name: "Upload documents" });
  await dialog
    .getByLabel("Choose files to upload")
    .setInputFiles(
      ["invoice-mismatch.pdf", "scanned-receipt.png", "ledger.xlsx", "corrupt.pdf"].map((f) =>
        join(FIXTURES, f),
      ),
    );
  await dialog.getByRole("button", { name: "Upload 4 files" }).click();
  await expect(dialog.getByText(/of 4 files accepted/)).toBeVisible({ timeout: 60_000 });
  await page.screenshot({ path: join(SHOTS, "ui-upload.png") });

  // ---- wait until the invoice PDF finished processing
  const pdfRow = dialog.getByRole("listitem").filter({ hasText: "invoice-mismatch.pdf" });
  await expect(pdfRow.getByText(/Needs review|Completed/)).toBeVisible({ timeout: PROCESSING_TIMEOUT });
  await pdfRow.getByRole("link", { name: "invoice-mismatch.pdf" }).click();
  await expect(page).toHaveURL(/\/documents\/[0-9a-f-]{36}/);

  // ---- review screen
  await expect(page.getByRole("heading", { name: "Issues" })).toBeVisible();
  await expect(page.getByText("TOTAL_MISMATCH")).toBeVisible();
  await page.screenshot({ path: join(SHOTS, "ui-review.png") });
  await expectNoSeriousA11yViolations(page, "review");

  // keyboard only: Apply suggestion, Ctrl+S to save
  const apply = page
    .locator("li")
    .filter({ hasText: "TOTAL_MISMATCH" })
    .getByRole("button", { name: "Apply suggestion" });
  await apply.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByLabel(/^Total/)).toHaveValue(/^270(\.0+)?$/);
  await page.getByRole("heading", { name: "Fields" }).click(); // move focus out of the input
  await page.keyboard.press("Control+s");
  await expect(page.getByText("TOTAL_MISMATCH")).toHaveCount(0, { timeout: 30_000 });

  // resolve any other open errors (e.g. a duplicate from an earlier run), keyboard only
  for (let round = 0; ; round++) {
    expect(round, "open error issues should run out").toBeLessThan(10);
    const resolve = page
      .locator("section[aria-label='Error issues'] li")
      .filter({ hasNot: page.getByText(/^Resolved/) })
      .getByRole("button", { name: "Resolve" })
      .first();
    if ((await resolve.count()) === 0) {
      break;
    }
    await resolve.focus();
    await page.keyboard.press("Enter");
    await page.keyboard.type("Checked against the paper original");
    await page.keyboard.press("Control+Enter"); // submits the note
    await expect(page.getByRole("dialog", { name: "Resolve issue" })).toHaveCount(0);
  }

  // A approves (a page-level shortcut: focus must not be in a field)
  await page.getByRole("heading", { name: "Fields" }).click();
  await expect(page.getByRole("button", { name: "Approve" })).toBeEnabled({ timeout: 30_000 });
  await page.keyboard.press("a");
  await expect(page.getByText("Approved", { exact: true }).first()).toBeVisible();

  // ---- documents list
  await page.getByRole("link", { name: "Documents" }).click();
  await expect(page.getByRole("table")).toBeVisible();
  await page.screenshot({ path: join(SHOTS, "ui-documents.png") });
  await expectNoSeriousA11yViolations(page, "documents list");

  // ---- search finds it (keyword: no quota)
  await page.getByRole("link", { name: "Search" }).click();
  await page.getByRole("radio", { name: "Keyword" }).click();
  await page.getByLabel("Search invoices").fill(INVOICE_NUMBER);
  await expect(page.getByRole("link", { name: `Invoice ${INVOICE_NUMBER}` }).first()).toBeVisible();
  await page.screenshot({ path: join(SHOTS, "ui-search.png") });
  await expectNoSeriousA11yViolations(page, "search");

  // ---- logout
  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page).toHaveURL(/\/login/);
  await page.goto("/documents");
  await expect(page).toHaveURL(/\/login/); // the session is really gone
});

test("the browser never talks to the backend directly", async ({ page }) => {
  const password = process.env.E2E_PASSWORD;
  test.skip(!password, "set E2E_PASSWORD (see .env.example)");
  const offOrigin: string[] = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.origin !== "http://localhost:3000") {
      offOrigin.push(request.url());
    }
  });
  await page.goto("/login");
  await page.getByLabel("Username").fill(process.env.E2E_USERNAME ?? "demo");
  await page.getByLabel("Password").fill(password ?? "");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: "Documents" })).toBeVisible();
  await page.goto("/search?q=invoice&mode=keyword");
  await expect(page.getByRole("heading", { name: "Search" })).toBeVisible();
  expect(offOrigin).toEqual([]);
});

test.describe("without JavaScript", () => {
  test.use({ javaScriptEnabled: false });

  test("login still works and credentials never go into a URL", async ({ page }) => {
    const password = process.env.E2E_PASSWORD;
    test.skip(!password, "set E2E_PASSWORD (see .env.example)");
    const urls: string[] = [];
    page.on("request", (request) => urls.push(request.url()));
    await page.goto("/login?next=%2Fsearch");
    await page.getByLabel("Username").fill("demo-wrong");
    await page.getByLabel("Password").fill("not-the-password");
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(page).toHaveURL(/\/login\?error=invalid/);
    await page.getByLabel("Username").fill(process.env.E2E_USERNAME ?? "demo");
    await page.getByLabel("Password").fill(password ?? "");
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(page).toHaveURL(/\/search/);
    expect(urls.filter((u) => u.includes("password") || (password ? u.includes(password) : false))).toEqual(
      [],
    );
  });
});
