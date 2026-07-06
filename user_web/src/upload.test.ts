import { beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "./api";
import { uploadPdf } from "./upload";

vi.mock("./api", () => ({ api: vi.fn() }));
const apiMock = vi.mocked(api);

describe("PDF upload", () => {
  beforeEach(() => apiMock.mockReset());

  it("uses direct upload for a small PDF", async () => {
    apiMock.mockResolvedValue({
      ok: true,
      uploaded: 1,
      paths: ["small.pdf"],
      errors: [],
    });
    const result = await uploadPdf(new File(["pdf"], "small.pdf"));
    expect(result).toBe("small.pdf");
    expect(apiMock).toHaveBeenCalledOnce();
    expect(apiMock.mock.calls[0][0]).toBe("/api/pdf/upload");
  });

  it("splits files larger than 8 MiB", async () => {
    apiMock
      .mockResolvedValueOnce({ ok: true, uploaded: 0, paths: [], errors: [] })
      .mockResolvedValueOnce({ ok: true, uploaded: 0, paths: [], errors: [] })
      .mockResolvedValueOnce({
        ok: true,
        uploaded: 1,
        paths: ["large.pdf"],
        errors: [],
      });
    const file = {
      name: "large.pdf",
      size: 17 * 1024 * 1024,
      slice: vi.fn(() => new Blob(["part"])),
    } as unknown as File;
    expect(await uploadPdf(file)).toBe("large.pdf");
    expect(apiMock).toHaveBeenCalledTimes(3);
    expect(apiMock.mock.calls.every(([path]) => path === "/api/pdf/upload-chunk")).toBe(true);
  });
});
