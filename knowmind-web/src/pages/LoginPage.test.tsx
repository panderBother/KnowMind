import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { LoginPage } from "@/pages/LoginPage";

describe("login form", () => {
  it("shows an API failure and lets the user retry", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(
      JSON.stringify({ detail: { message: "邮箱或密码错误" } }), { status: 401 },
    ));
    render(<MemoryRouter><LoginPage /></MemoryRouter>);
    fireEvent.change(screen.getByLabelText("邮箱"), { target: { value: "qa@example.com" } });
    fireEvent.change(screen.getByLabelText("密码", { exact: true }), { target: { value: "wrong-password" } });
    fireEvent.submit(screen.getByLabelText("邮箱").closest("form")!);
    expect(await screen.findByRole("alert")).toHaveTextContent("邮箱或密码错误");
    await waitFor(() => expect(screen.getAllByRole("button", { name: /^登录$/ })[1]).toBeEnabled());
    expect(localStorage.getItem("knowmind_access_token")).toBeNull();
  });
});
