import { expect, test } from "@playwright/test";

test("protected pages redirect unauthenticated users", async ({ page }) => {
  await page.goto("/quality");
  await expect(page).toHaveURL(/\/login$/);
  await expect(page.getByLabel("邮箱")).toBeVisible();
});

test("login failure remains on login and displays the API error", async ({ page }) => {
  await page.route("**/api/v1/auth/login", (route) => route.fulfill({
    status: 401, json: { detail: "邮箱或密码错误" },
  }));
  await page.goto("/login");
  await page.getByLabel("邮箱").fill("qa@example.com");
  await page.getByLabel("密码", { exact: true }).fill("incorrect-password");
  await page.locator("form").getByRole("button", { name: "登录", exact: true }).click();
  await expect(page.getByRole("alert")).toHaveText("邮箱或密码错误");
  await expect(page).toHaveURL(/\/login$/);
});

test("login, stream a chat answer, and logout", async ({ page }) => {
  await page.route("**/api/v1/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    if (path.endsWith("/auth/login")) {
      await route.fulfill({
        json: {
          user: { id: "user-1", email: "qa@example.com", created_at: "2026-01-01T00:00:00Z" },
          access_token: "e2e-access",
          refresh_token: "e2e-refresh",
          token_type: "bearer",
          expires_in: 3600,
        },
      });
      return;
    }
    if (path.endsWith("/auth/logout")) {
      await route.fulfill({ status: 204 });
      return;
    }
    if (path.endsWith("/chat/stream")) {
      await route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        body: [
          'data: {"type":"trace_id","trace_id":"trace-e2e"}',
          'data: {"type":"conversation_id","conversation_id":"conv-e2e","is_new":true}',
          'data: {"type":"user_message_saved","message_id":"msg-user"}',
          'data: {"type":"message_saved","message_id":"msg-ai"}',
          'data: {"type":"delta","text":"这是端到端测试回答"}',
          'data: {"type":"done"}',
          "",
        ].join("\n\n"),
      });
      return;
    }
    if (path.endsWith("/knowledge-bases") || path.endsWith("/conversations") || path.endsWith("/mcp/tools")) {
      await route.fulfill({ json: [] });
      return;
    }
    await route.fulfill({ json: [] });
  });

  await page.goto("/login");
  await page.getByPlaceholder("you@university.edu").fill("qa@example.com");
  await page.getByPlaceholder("••••••••").fill("password123");
  await page.locator("form").getByRole("button", { name: "登录", exact: true }).click();
  await expect(page).toHaveURL(/\/knowledge-bases$/);

  await page.getByRole("link", { name: "智能对话" }).click();
  await page.locator('textarea[placeholder*="输入问题"]:visible').fill("请回答测试问题");
  await page.locator("button:visible").filter({ hasText: /^发送$/ }).click();
  await expect(page.locator("p:visible").filter({ hasText: "这是端到端测试回答" })).toBeVisible();

  await page.getByRole("button", { name: "退出登录" }).click();
  await expect(page).toHaveURL(/\/login$/);
  const accessToken = await page.evaluate(() => localStorage.getItem("knowmind_access_token"));
  expect(accessToken).toBeNull();
});
