import { describe, expect, it } from "vitest";

import { formatBytes, formatDuration, formatInvoiceDate, formatTimestamp } from "./dates";
import { fromScaled, toScaled } from "./decimal";
import { formatDecimal, formatMoney } from "./money";

describe("formatMoney", () => {
  it.each([
    ["1234.5600", "USD", "en-US", "USD 1,234.56"],
    ["1234.5600", "EUR", "de-DE", "1.234,56 EUR"],
    ["1234.5600", "JPY", "en-US", "JPY 1,235"], // 0 minor units
    ["1.2345", "KWD", "en-US", "KWD 1.235"], // 3 minor units
    ["0.105", "USD", "en-US", "USD 0.11"], // a float would give 0.10
    ["99999999999999.9999", "USD", "en-US", "USD 100,000,000,000,000.00"],
    ["-50.00", "EUR", "en-US", "-EUR 50.00"],
    ["12.5", null, "en-US", "12.50"],
  ])("%s %s (%s) -> %s", (amount, currency, locale, expected) => {
    expect(formatMoney(amount, currency, locale).replace(/ /g, " ")).toBe(expected);
  });

  it("keeps exact precision on huge values (no float rounding)", () => {
    expect(formatMoney("9007199254740993.01", "USD", "en-US")).toContain("9,007,199,254,740,993.01");
  });

  it.each([
    [null, "—"],
    [undefined, "—"],
    ["", "—"],
    ["not a number", "not a number"],
  ])("%s -> %s", (amount, expected) => {
    expect(formatMoney(amount, "USD", "en-US")).toBe(expected);
  });

  it("falls back to plain formatting for an invalid currency code", () => {
    expect(formatMoney("10", "usd", "en-US")).toBe("10.00");
  });

  it("formats plain decimals", () => {
    expect(formatDecimal("1234.5000", "en-US")).toBe("1,234.5");
  });
});

describe("dates", () => {
  it.each([
    ["2026-03-01", "en-US", "Mar 1, 2026"],
    ["2026-03-01", "en-GB", "1 Mar 2026"],
    ["2026-12-31", "en-US", "Dec 31, 2026"],
  ])("invoice date %s in %s", (iso, locale, expected) => {
    expect(formatInvoiceDate(iso, locale)).toBe(expected);
  });

  it("never shifts a calendar date across time zones", () => {
    // formatted in UTC: no "Feb 28" west of Greenwich
    expect(formatInvoiceDate("2026-03-01", "en-US")).toBe("Mar 1, 2026");
  });

  it("passes unknown formats through", () => {
    expect(formatInvoiceDate("01/03/2026")).toBe("01/03/2026");
    expect(formatInvoiceDate(null)).toBe("—");
  });

  it("shows timestamps in the given zone", () => {
    expect(formatTimestamp("2026-03-01T23:30:00Z", "en-US", "UTC")).toBe("Mar 1, 2026, 11:30 PM");
    expect(formatTimestamp("2026-03-01T23:30:00Z", "en-US", "Asia/Dhaka")).toBe("Mar 2, 2026, 5:30 AM");
  });

  it.each([
    [null, "—"],
    [250, "250 ms"],
    [4200, "4.2 s"],
    [125_000, "2 min 5 s"],
  ])("duration %s -> %s", (ms, expected) => {
    expect(formatDuration(ms)).toBe(expected);
  });

  it.each([
    [512, "512 B"],
    [2048, "2.0 KB"],
    [5 * 1024 * 1024, "5.0 MB"],
  ])("bytes %s -> %s", (bytes, expected) => {
    expect(formatBytes(bytes)).toBe(expected);
  });
});

describe("decimal", () => {
  it("round-trips exactly", () => {
    expect(toScaled("0.1")).toBe(10_000_000n);
    expect(fromScaled(toScaled("1234.005") ?? 0n)).toBe("1234.01");
    expect(fromScaled(toScaled("-0.004") ?? 0n)).toBe("0.00");
    expect(toScaled("1e5")).toBeNull();
    expect(toScaled("1.123456789")).toBeNull();
  });
});
