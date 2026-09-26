/**
 * JWT 与鉴权相关 API（与 FastAPI `/api/v1/auth` 对齐）。
 */

import { apiFetch, apiJson, parseApiError } from "@/services/http";

export const ACCESS_TOKEN_KEY = "knowmind_access_token";
export const REFRESH_TOKEN_KEY = "knowmind_refresh_token";

export function getAccessToken(): string | null {
  return localStorage.getItem(ACCESS_TOKEN_KEY);
}

export function setAccessToken(token: string): void {
  localStorage.setItem(ACCESS_TOKEN_KEY, token);
}

export function setRefreshToken(token: string): void {
  localStorage.setItem(REFRESH_TOKEN_KEY, token);
}

export function getRefreshToken(): string | null {
  return localStorage.getItem(REFRESH_TOKEN_KEY);
}

export function clearAccessToken(): void {
  localStorage.removeItem(ACCESS_TOKEN_KEY);
  localStorage.removeItem(REFRESH_TOKEN_KEY);
}

let sessionExpiredRedirecting = false;

/** 登录失效：清除令牌并跳转登录页 */
export function handleSessionExpired(): void {
  if (sessionExpiredRedirecting) return;
  if (typeof window !== "undefined" && window.location.pathname.startsWith("/login")) return;
  sessionExpiredRedirecting = true;
  clearAccessToken();
  window.location.replace("/login");
}

export type UserPublic = {
  id: string;
  email: string;
  created_at: string;
};

export type AuthOkResponse = {
  user: UserPublic;
  access_token: string;
  refresh_token?: string | null;
  token_type: string;
  expires_in: number;
  refresh_expires_in?: number | null;
};

export function storeAuthTokens(res: AuthOkResponse): void {
  setAccessToken(res.access_token);
  if (res.refresh_token) setRefreshToken(res.refresh_token);
}

export async function authRegister(email: string, password: string): Promise<AuthOkResponse> {
  return apiJson<AuthOkResponse>("/auth/register", {
    method: "POST",
    skipAuth: true,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
}

export async function authLogin(email: string, password: string): Promise<AuthOkResponse> {
  return apiJson<AuthOkResponse>("/auth/login", {
    method: "POST",
    skipAuth: true,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
}

export async function authMe(): Promise<UserPublic> {
  if (!getAccessToken()) throw new Error("未登录");
  return apiJson<UserPublic>("/auth/me");
}

/** 刷新令牌须直连，避免与 http.ts 401 重试形成环 */
export async function authRefresh(): Promise<AuthOkResponse> {
  const refresh = getRefreshToken();
  if (!refresh) throw new Error("无刷新令牌");
  const res = await fetch("/api/v1/auth/refresh", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ refresh_token: refresh }),
  });
  if (!res.ok) throw new Error(await parseApiError(res));
  const data = (await res.json()) as AuthOkResponse;
  storeAuthTokens(data);
  return data;
}

export async function authChangePassword(currentPassword: string, newPassword: string): Promise<void> {
  if (!getAccessToken()) throw new Error("未登录");
  const res = await apiFetch("/auth/change-password", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }),
  });
  if (!res.ok) throw new Error(await parseApiError(res));
}

export async function authLogout(): Promise<void> {
  try {
    const res = await apiFetch("/auth/logout", { method: "POST" });
    if (!res.ok && res.status !== 401) throw new Error(await parseApiError(res));
  } finally {
    clearAccessToken();
  }
}
