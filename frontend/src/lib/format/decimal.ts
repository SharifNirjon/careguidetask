/**
 * Exact decimal arithmetic on strings, via BigInt scaled by 10^SCALE. Used only
 * for UX hints (line arithmetic); the server does the real validation.
 * Never routes money through floats.
 */
const SCALE = 8;
const FACTOR = 10n ** BigInt(SCALE);
const DECIMAL = /^(-)?(\d+)(?:\.(\d+))?$/;

/** Scaled BigInt, or null for empty / invalid / too-precise input. */
export function toScaled(value: string): bigint | null {
  const match = DECIMAL.exec(value.trim());
  if (!match) {
    return null;
  }
  const [, sign, whole, fraction = ""] = match;
  if (fraction.length > SCALE || whole === undefined) {
    return null;
  }
  const scaled = BigInt(whole) * FACTOR + BigInt(fraction.padEnd(SCALE, "0"));
  return sign ? -scaled : scaled;
}

export function multiply(a: bigint, b: bigint): bigint {
  return (a * b) / FACTOR; // truncates beyond 8 decimals: fine for a hint
}

export function absolute(a: bigint): bigint {
  return a < 0n ? -a : a;
}

export function fromScaled(value: bigint, fractionDigits = 2): string {
  const negative = value < 0n;
  const abs = negative ? -value : value;
  const divisor = 10n ** BigInt(SCALE - fractionDigits);
  const rounded = (abs + divisor / 2n) / divisor; // half-up at the requested precision
  const text = rounded.toString().padStart(fractionDigits + 1, "0");
  const whole = text.slice(0, text.length - fractionDigits);
  const fraction = text.slice(text.length - fractionDigits);
  return `${negative && rounded !== 0n ? "-" : ""}${whole}${fractionDigits ? `.${fraction}` : ""}`;
}

/** One cent: per-line rounding on invoices is up to half a minor unit each way. */
export const HINT_TOLERANCE = toScaled("0.01") ?? 0n;
