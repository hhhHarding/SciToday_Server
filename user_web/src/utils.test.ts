import { describe, expect, it, vi } from "vitest";
import type { Digest } from "./types";
import { displayTime, displayTitle, uploadId } from "./utils";

const digest = {
  filename: "digest.html",
  timestamp: "20260706_123456",
  title: "English",
  cn_title: "中文标题",
} as Digest;

describe("display helpers", () => {
  it("prefers the translated title and formats backend timestamps", () => {
    expect(displayTitle(digest)).toBe("中文标题");
    expect(displayTime(digest.timestamp)).toBe("2026-07-06 12:34");
  });

  it("creates an opaque upload id", () => {
    vi.spyOn(crypto, "getRandomValues").mockImplementation((value) => {
      const target = value as Uint32Array;
      target.set([1, 2, 3, 4]);
      return value;
    });
    expect(uploadId(new File([], "paper.pdf"))).toBe(
      "00000001000000020000000300000004",
    );
  });
});
