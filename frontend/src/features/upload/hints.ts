/**
 * Client-side checks are HINTS only: they warn early, but the server decides
 * (it sniffs the real type from the bytes and enforces the real limits).
 */
export const ACCEPTED_EXTENSIONS = [
  ".pdf",
  ".png",
  ".jpg",
  ".jpeg",
  ".tif",
  ".tiff",
  ".xlsx",
  ".xls",
  ".csv",
] as const;
export const HINT_MAX_FILE_MB = 100;
export const HINT_MAX_FILES = 20;
const BYTES_PER_MB = 1024 * 1024;

export function fileHint(file: File): string | null {
  const name = file.name.toLowerCase();
  if (!ACCEPTED_EXTENSIONS.some((ext) => name.endsWith(ext))) {
    return "This type is probably not supported (PDF, images, Excel or CSV).";
  }
  if (file.size > HINT_MAX_FILE_MB * BYTES_PER_MB) {
    return `Larger than ${HINT_MAX_FILE_MB} MB: the server will probably reject it.`;
  }
  if (file.size === 0) {
    return "The file is empty.";
  }
  return null;
}

export function batchHint(count: number): string | null {
  return count > HINT_MAX_FILES
    ? `At most ${HINT_MAX_FILES} files per upload: split them into batches.`
    : null;
}
