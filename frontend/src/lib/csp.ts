/**
 * Content-Security-Policy. Scripts need a per-request nonce ('strict-dynamic'
 * lets Next's nonce'd loader pull in its chunks). Styles allow 'unsafe-inline':
 * Radix positions popovers with inline style attributes; CSS can't run code.
 * Everything else (images, API calls) is same-origin only: the browser only
 * ever talks to this app, never to the backend directly.
 */
export function buildCsp(nonce: string, dev: boolean): string {
  const directives = [
    "default-src 'self'",
    `script-src 'self' 'nonce-${nonce}' 'strict-dynamic'${dev ? " 'unsafe-eval'" : ""}`,
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' blob: data:",
    "font-src 'self'",
    `connect-src 'self'${dev ? " ws:" : ""}`,
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
  ];
  return directives.join("; ");
}
