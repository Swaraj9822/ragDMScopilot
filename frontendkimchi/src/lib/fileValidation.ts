import { ACCEPTED_EXTENSIONS, MAX_UPLOAD_BYTES } from "./constants";

export interface FileValidationResult {
  ok: boolean;
  error: string | null;
}

export function getExtension(filename: string): string {
  const dot = filename.lastIndexOf(".");
  if (dot < 0) return "";
  return filename.slice(dot + 1).toLowerCase();
}

export function validateFile(file: File): FileValidationResult {
  const ext = getExtension(file.name);
  if (!ext || !(ACCEPTED_EXTENSIONS as readonly string[]).includes(ext)) {
    return {
      ok: false,
      error: `Unsupported format: .${ext || "(none)"}. See accepted formats.`,
    };
  }
  if (file.size === 0) {
    return { ok: false, error: "File is empty." };
  }
  if (file.size > MAX_UPLOAD_BYTES) {
    return { ok: false, error: "This file exceeds the 10 MiB client limit." };
  }
  return { ok: true, error: null };
}
