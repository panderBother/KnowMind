import { describe, expect, it } from "vitest";

import {
  clearAccessToken,
  getAccessToken,
  getRefreshToken,
  storeAuthTokens,
} from "@/services/auth";

describe("auth token storage", () => {
  it("stores and revokes both access and refresh tokens", () => {
    storeAuthTokens({
      user: { id: "u1", email: "user@example.com", created_at: "2026-01-01T00:00:00Z" },
      access_token: "access",
      refresh_token: "refresh",
      token_type: "bearer",
      expires_in: 3600,
    });
    expect(getAccessToken()).toBe("access");
    expect(getRefreshToken()).toBe("refresh");

    clearAccessToken();
    expect(getAccessToken()).toBeNull();
    expect(getRefreshToken()).toBeNull();
  });
});
