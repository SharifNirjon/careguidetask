/**
 * Money arrives as a decimal STRING ("1234.5600") and is never converted to a
 * JS number: `Intl.NumberFormat.prototype.format` accepts numeric strings and
 * keeps their exact decimal value (ES2023 Intl.NumberFormat v3), so
 * "0.105" rounds to "0.11", where a float would give "0.10". Each currency's
 * minor units come from Intl (JPY 0, KWD 3, most others 2).
 */

const DECIMAL = /^-?\d+(\.\d+)?$/;
const PLACEHOLDER = "—";

type NumericString = `${number}`;

function isDecimal(value: string): value is NumericString {
  return DECIMAL.test(value);
}

function validCurrency(currency: string | null | undefined): currency is string {
  return typeof currency === "string" && /^[A-Z]{3}$/.test(currency);
}

export function formatMoney(
  amount: string | null | undefined,
  currency?: string | null,
  locale?: string,
): string {
  if (amount === null || amount === undefined || amount === "") {
    return PLACEHOLDER;
  }
  if (!isDecimal(amount)) {
    return amount; // never hide what the server sent
  }
  const options: Intl.NumberFormatOptions = validCurrency(currency)
    ? { style: "currency", currency, currencyDisplay: "code" }
    : { minimumFractionDigits: 2, maximumFractionDigits: 4 };
  try {
    return new Intl.NumberFormat(locale, options).format(amount);
  } catch {
    return validCurrency(currency) ? `${amount} ${currency}` : amount;
  }
}

/** Plain decimal (quantities, rates), up to 4 fraction digits, no currency. */
export function formatDecimal(value: string | null | undefined, locale?: string): string {
  if (value === null || value === undefined || value === "") {
    return PLACEHOLDER;
  }
  if (!isDecimal(value)) {
    return value;
  }
  return new Intl.NumberFormat(locale, { maximumFractionDigits: 4 }).format(value);
}
