export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public retryAfter = 0,
  ) {
    super(message);
  }
}

function csrfToken(): string {
  const match = document.cookie.match(/(?:^|;\s*)scitoday_csrf=([^;]+)/);
  return match ? decodeURIComponent(match[1]) : "";
}

export async function api<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const headers = new Headers(options.headers || {});
  const method = (options.method || "GET").toUpperCase();
  if (options.body && !(options.body instanceof FormData) && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  if (!["GET", "HEAD", "OPTIONS"].includes(method)) {
    const csrf = csrfToken();
    if (csrf) headers.set("X-CSRF-Token", csrf);
  }
  const response = await fetch(path, {
    ...options,
    headers,
    credentials: "same-origin",
  });
  const text = await response.text();
  let body: unknown = {};
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = { error: text };
    }
  }
  if (!response.ok) {
    const data = body as { error?: string; message?: string };
    const code = data.error || `http_${response.status}`;
    const message = data.message || friendlyError(response.status, code);
    if (response.status === 401) {
      window.dispatchEvent(new CustomEvent("scitoday:unauthorized"));
    }
    throw new ApiError(
      response.status,
      code,
      message,
      Number(response.headers.get("Retry-After") || 0),
    );
  }
  return body as T;
}

export async function loginWithToken(token: string): Promise<unknown> {
  return api("/api/web/session", {
    method: "POST",
    headers: { Authorization: `Bearer ${token}` },
  });
}

function friendlyError(status: number, code: string): string {
  if (status === 401) return "登录已失效，请重新登录";
  if (status === 403 && code === "csrf_failed") return "页面安全凭据已失效，请刷新后重试";
  if (status === 403) return "当前账号没有执行此操作的权限";
  if (status === 409) return "任务正在运行，请稍后再试";
  if (status === 429) return "操作过于频繁，请稍后再试";
  return code || `请求失败（${status}）`;
}

export function jsonBody(value: unknown): string {
  return JSON.stringify(value);
}
