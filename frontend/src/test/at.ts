/** Element `index` of `items`, or a test failure (instead of a non-null assertion). */
export function at<T>(items: readonly T[], index: number): T {
  const item = items[index];
  if (item === undefined) {
    throw new Error(`expected an element at index ${index}`);
  }
  return item;
}

/** A defined value, or a test failure. */
export function defined<T>(value: T | undefined | null, what = "value"): T {
  if (value === undefined || value === null) {
    throw new Error(`expected ${what} to be defined`);
  }
  return value;
}
