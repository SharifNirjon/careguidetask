import type { DocumentDetail, ExtractionOut, FieldOut, IssueOut } from "@/lib/api/types";

const NOW = "2026-03-01T12:00:00Z";
export const DOCUMENT_ID = "11111111-1111-4111-8111-111111111111";
export const EXTRACTION_ID = "22222222-2222-4222-8222-222222222222";

export function field(value: string | null, overrides: Partial<FieldOut> = {}): FieldOut {
  return {
    value,
    raw_value: value,
    confidence: 0.95,
    grounded: true,
    evidence: value ? `… ${value} …` : null,
    unit_index: 0,
    source: "llm",
    ...overrides,
  };
}

export function issue(overrides: Partial<IssueOut> = {}): IssueOut {
  return {
    id: "33333333-3333-4333-8333-333333333333",
    extraction_id: EXTRACTION_ID,
    rule_code: "TOTAL_MISMATCH",
    severity: "error",
    field_path: "total",
    message: "Subtotal 225.00 + tax 45.00 = 270.00, but total is 300.00",
    expected_value: "270.00",
    actual_value: "300.00",
    related_extraction_id: null,
    resolved: false,
    resolved_at: null,
    resolved_by: null,
    resolution_note: null,
    created_at: NOW,
    ...overrides,
  };
}

export function extraction(overrides: Partial<ExtractionOut> = {}): ExtractionOut {
  return {
    id: EXTRACTION_ID,
    document_id: DOCUMENT_ID,
    invoice_index: 0,
    unit_start: 0,
    unit_end: 0,
    version: 1,
    is_current: true,
    lock_version: 3,
    document_type: "invoice",
    review_status: "needs_review",
    reviewed_by: null,
    reviewed_at: null,
    review_note: null,
    overall_confidence: 0.9,
    model_name: "test-model",
    prompt_version: "v1",
    fields: {
      invoice_number: field("INV-2026-001"),
      invoice_date: field("2026-03-01"),
      supplier_name: field("Acme Ltd"),
      customer_name: field("Beta Corp"),
      subtotal: field("225.0000"),
      tax: field("45.0000"),
      total: field("300.0000"),
      currency: field("EUR"),
    },
    line_items: [
      {
        id: "44444444-4444-4444-8444-444444444444",
        position: 0,
        description: "Consulting",
        quantity: "10.0000",
        unit_price: "22.5000",
        amount: "225.0000",
        tax_rate: null,
        fields: {},
      },
    ],
    issues: [issue()],
    reconciliations: [],
    created_at: NOW,
    updated_at: NOW,
    ...overrides,
  };
}

export function document(overrides: Partial<DocumentDetail> = {}): DocumentDetail {
  return {
    id: DOCUMENT_ID,
    filename: "invoice.pdf",
    file_type: "pdf",
    mime_type: "application/pdf",
    size_bytes: 1000,
    sha256: "a".repeat(64),
    status: "needs_review",
    error: null,
    page_count: 1,
    unit_count: 1,
    uploaded_by: "demo",
    duplicate_of_document_id: null,
    created_at: NOW,
    updated_at: NOW,
    processing_started_at: NOW,
    processing_finished_at: NOW,
    latest_job: null,
    progress: {
      stage: null,
      stages_done: 5,
      stages_total: 5,
      units_done: 1,
      units_total: 1,
      percent: 100,
      started_at: NOW,
      elapsed_ms: 4200,
    },
    ...overrides,
  };
}
