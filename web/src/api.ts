/** 唯一的数据入口：统一信封、CSRF、401 处理。 */

export type ApiErrorBody = { code: string; message: string; hint?: string };

export class ApiError extends Error {
  code: string;
  status: number;
  hint?: string;
  constructor(message: string, code: string, status: number, hint?: string) {
    super(message);
    this.code = code;
    this.status = status;
    this.hint = hint;
  }
}

let csrf = "";
let onUnauthorized: (() => void) | null = null;

export function setCsrf(value: string) {
  csrf = value;
}

export function setUnauthorizedHandler(fn: (() => void) | null) {
  onUnauthorized = fn;
}

type Init = Omit<RequestInit, "body"> & { body?: unknown };

export async function api<T>(path: string, init: Init = {}): Promise<T> {
  const method = (init.method ?? "GET").toUpperCase();
  const headers: Record<string, string> = { ...(init.headers as Record<string, string>) };
  let body: BodyInit | undefined;
  if (init.body !== undefined) {
    headers["content-type"] = "application/json";
    body = JSON.stringify(init.body);
  }
  if (method !== "GET" && method !== "HEAD") headers["X-Haiq-CSRF"] = csrf;

  const res = await fetch(path, {
    ...init,
    method,
    headers,
    body,
    credentials: "same-origin",
  });

  let payload: unknown = null;
  try {
    payload = await res.json();
  } catch {
    payload = null;
  }
  const envelope = payload as { ok?: boolean; data?: unknown; error?: ApiErrorBody } | null;

  if (!res.ok || envelope?.ok === false) {
    const err = envelope?.error;
    if (res.status === 401) onUnauthorized?.();
    throw new ApiError(
      err?.message ?? `请求失败（HTTP ${res.status}）`,
      err?.code ?? "http_error",
      res.status,
      err?.hint,
    );
  }
  return (envelope?.data ?? null) as T;
}

export const get = <T>(path: string) => api<T>(path);
export const post = <T>(path: string, body?: unknown) => api<T>(path, { method: "POST", body });
export const del = <T>(path: string) => api<T>(path, { method: "DELETE" });
