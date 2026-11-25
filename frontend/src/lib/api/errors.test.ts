import { describe, expect, it } from "vitest";

import { AppError, messageFor, networkError, toAppError } from "./errors";

const problem = {
  type: "https://careguide.dev/problems/invalid_values",
  title: "Unprocessable Content",
  status: 422,
  detail: "Some values could not be parsed.",
  instance: "/api/v1/extractions/x",
  code: "invalid_values",
  request_id: "req-123",
  errors: [
    { loc: ["body", "fields", "total"], msg: "not an amount: 'twelve'", type: "invalid_total" },
    { loc: ["body", "line_items", 0, "amount"], msg: "not an amount", type: "invalid_amount" },
  ],
};

describe("toAppError", () => {
  it("maps problem+json, including field errors and the request id", () => {
    const error = toAppError(422, problem, "header-id");
    expect(error).toBeInstanceOf(AppError);
    expect(error).toMatchObject({ status: 422, code: "invalid_values", requestId: "req-123" });
    expect(error.fieldMessages()).toEqual({ total: "not an amount: 'twelve'" });
    expect(error.fieldMessages(["body", "line_items"])).toEqual({ "0.amount": "not an amount" });
    expect(error.userMessage).toBe("Some values are not valid. Check the highlighted fields.");
  });

  it("falls back to the response header request id and the server's detail", () => {
    const error = toAppError(409, { code: "something_new", detail: "Server says no." }, "header-id");
    expect(error.requestId).toBe("header-id");
    expect(error.userMessage).toBe("Server says no.");
  });

  it("handles bodies that are not JSON objects", () => {
    const error = toAppError(502, null, null);
    expect(error).toMatchObject({ code: "http_502", requestId: null });
    expect(error.userMessage).toBe("Something went wrong. Please try again.");
  });

  it("ignores malformed field errors", () => {
    expect(toAppError(422, { code: "x", errors: [{ loc: "nope" }, 3] }).fieldErrors).toEqual([
      { loc: [], msg: "Invalid value", type: "value_error" },
    ]);
  });
});

describe("messages", () => {
  it.each([
    ["TYPE_MISMATCH", "The file's content doesn't match its extension."],
    ["FILE_TOO_LARGE", "The file is larger than the upload limit."],
    ["PASSWORD_PROTECTED", "The file is password protected. Remove the password and upload it again."],
    [
      "LLM_QUOTA_DEFERRED",
      "Today's extraction capacity is used up. The file will be processed after the daily reset.",
    ],
    ["version_mismatch", "Someone else changed this invoice. Reload to see their changes."],
  ])("%s has a friendly message", (code, message) => {
    expect(messageFor(code)).toBe(message);
  });

  it("network errors are status 0 with a friendly message", () => {
    expect(networkError(new Error("x"))).toMatchObject({ status: 0, code: "network_error" });
  });
});
