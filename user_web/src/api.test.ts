import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, api } from "./api";

describe("api client", () => {
  beforeEach(() => {
    document.cookie = "scitoday_csrf=test-csrf; path=/";
    vi.restoreAllMocks();
  });

  it("sends credentials and CSRF on mutating requests", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response('{"ok":true}', {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    await api("/api/test", { method: "POST", body: "{}" });
    const request = fetchMock.mock.calls[0][1] as RequestInit;
    expect(request.credentials).toBe("same-origin");
    expect(new Headers(request.headers).get("X-CSRF-Token")).toBe("test-csrf");
  });

  it("signals an expired session on 401", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response('{"error":"unauthorized"}', { status: 401 }),
    );
    const listener = vi.fn();
    window.addEventListener("scitoday:unauthorized", listener, { once: true });
    await expect(api("/api/test")).rejects.toBeInstanceOf(ApiError);
    expect(listener).toHaveBeenCalledOnce();
  });
});
