import { describe, expect, it } from "vitest";
import { getExtension, validateFile } from "./fileValidation";
import { MAX_UPLOAD_BYTES } from "./constants";

function makeFile(name: string, size: number): File {
  const file = new File(["x"], name);
  Object.defineProperty(file, "size", { value: size });
  return file;
}

describe("getExtension", () => {
  it("extracts lowercase extension", () => {
    expect(getExtension("Report.PDF")).toBe("pdf");
    expect(getExtension("archive.tar.gz")).toBe("gz");
    expect(getExtension("noext")).toBe("");
  });
});

describe("validateFile", () => {
  it("accepts a supported, non-empty, within-limit file", () => {
    expect(validateFile(makeFile("doc.pdf", 1024))).toEqual({ ok: true, error: null });
  });

  it("rejects an unsupported extension", () => {
    const result = validateFile(makeFile("malware.exe", 1024));
    expect(result.ok).toBe(false);
    expect(result.error).toMatch(/unsupported/i);
  });

  it("rejects an empty file", () => {
    const result = validateFile(makeFile("empty.txt", 0));
    expect(result.ok).toBe(false);
    expect(result.error).toMatch(/empty/i);
  });

  it("rejects a file over the client size limit", () => {
    const result = validateFile(makeFile("big.pdf", MAX_UPLOAD_BYTES + 1));
    expect(result.ok).toBe(false);
    expect(result.error).toMatch(/exceeds/i);
  });
});
