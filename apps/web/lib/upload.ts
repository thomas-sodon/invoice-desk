/** Client-side upload checks mirroring the API limits (M1): JPEG, PNG, HEIC, PDF; at most 10 MB. */
export const MAX_UPLOAD_BYTES = 10 * 1024 * 1024;
export const MAX_PAGES = 5;
export const ACCEPT_ATTR = "image/jpeg,image/png,image/heic,image/heif,application/pdf,.heic,.heif,.jpg,.jpeg,.png,.pdf";

const MIME_OK = new Set(["image/jpeg", "image/png", "image/heic", "image/heif", "application/pdf"]);
const EXT_OK = /\.(jpe?g|png|heic|heif|pdf)$/i;

export interface FileLike {
  name: string;
  size: number;
  type: string;
}

/** Returns a plain-English reason the file can't be uploaded, or null when it is acceptable. */
export function uploadProblem(f: FileLike): string | null {
  const genericType = f.type === "" || f.type === "application/octet-stream";
  const typeOk = MIME_OK.has(f.type.toLowerCase()) || (genericType && EXT_OK.test(f.name));
  if (!typeOk) return `${f.name}: only photos (JPEG, PNG, HEIC) and PDFs can be read.`;
  if (f.size > MAX_UPLOAD_BYTES) return `${f.name}: larger than 10 MB.`;
  if (f.size === 0) return `${f.name}: the file is empty.`;
  return null;
}

export function splitUploads<T extends FileLike>(files: T[]): { ok: T[]; rejected: { filename: string; error: string }[] } {
  const ok: T[] = [];
  const rejected: { filename: string; error: string }[] = [];
  for (const f of files) {
    const p = uploadProblem(f);
    if (p) rejected.push({ filename: f.name, error: p });
    else ok.push(f);
  }
  return { ok, rejected };
}

export function isPdfName(name: string): boolean {
  return /\.pdf$/i.test(name);
}
