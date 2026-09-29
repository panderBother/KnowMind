import { useState } from "react";
import { useNavigate } from "react-router-dom";
import clsx from "clsx";
import { BookOpen, Eye, EyeOff, Microscope } from "lucide-react";

import { authLogin, authRegister, storeAuthTokens } from "@/services/auth";

/**
 * 登录 / 注册：桌面端分屏品牌区；移动端顶部品牌 + 插画占位 + 单卡片表单（对齐移动原型）。
 */
export function LoginPage() {
  const nav = useNavigate();
  const [tab, setTab] = useState<"login" | "register">("login");
  const [showPw, setShowPw] = useState(false);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [errMsg, setErrMsg] = useState<string | null>(null);

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setErrMsg(null);
    setBusy(true);
    try {
      const res =
        tab === "login"
          ? await authLogin(email.trim(), password)
          : await authRegister(email.trim(), password);
      storeAuthTokens(res);
      nav("/knowledge-bases", { replace: true });
    } catch (err) {
      setErrMsg(err instanceof Error ? err.message : "请求失败");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex min-h-[100dvh]">
      <section className="relative hidden w-1/2 flex-col justify-between bg-gradient-to-br from-[#0052CC] via-primary to-[#66A3FF] p-10 text-white lg:flex">
        <div>
          <div className="text-2xl font-bold tracking-tight">KnowMind AI</div>
          <p className="mt-2 max-w-md text-sm text-blue-100">
            面向知识工作流的 RAG + Agent：私有知识库、可追溯引用与评估闭环。
          </p>
        </div>
        <ul className="space-y-4 text-sm">
          <li className="flex gap-2">
            <span className="mt-0.5 h-2 w-2 rounded-full bg-white/80" />
            私有知识库：文档向量化与权限隔离
          </li>
          <li className="flex gap-2">
            <span className="mt-0.5 h-2 w-2 rounded-full bg-white/80" />
            AI 深度研究：分解任务、检索、重排与报告生成
          </li>
          <li className="flex gap-2">
            <span className="mt-0.5 h-2 w-2 rounded-full bg-white/80" />
            可追溯来源：段落级引用与 PDF 回链
          </li>
        </ul>
        <div className="text-xs text-blue-100">© KnowMind</div>
      </section>

      <section className="flex flex-1 flex-col bg-slate-50 lg:items-center lg:justify-center lg:p-6">
        {/* 移动端：顶部品牌 + 轻量「插画」占位（可用 assets 替换为正式图） */}
        <div className="relative overflow-hidden bg-gradient-to-b from-primary to-[#3385FF] px-6 pb-10 pt-8 text-white lg:hidden">
          <div className="text-center">
            <div className="text-xl font-bold tracking-tight">KnowMind AI</div>
            <p className="mt-1 text-xs text-blue-100">AI 驱动的私有知识助手</p>
          </div>
          <div className="mx-auto mt-6 flex h-36 max-w-xs items-center justify-center gap-4 rounded-2xl bg-white/10 backdrop-blur-sm">
            <Microscope className="h-14 w-14 text-white/90 drop-shadow-md" strokeWidth={1.25} />
            <BookOpen className="h-12 w-12 text-white/80 drop-shadow" strokeWidth={1.25} />
          </div>
        </div>

        <div className="mx-4 -mt-6 mb-8 flex-1 rounded-2xl border border-slate-200 bg-white px-6 pb-8 pt-6 shadow-card lg:mx-0 lg:mt-0 lg:mb-0 lg:max-w-md lg:flex-none lg:px-8 lg:py-8">
            <div className="mb-6 flex rounded-lg bg-slate-100 p-1 text-sm font-medium">
              <button
                type="button"
                className={clsx(
                  "flex-1 rounded-md py-2 transition",
                  tab === "login" ? "bg-white text-slate-900 shadow-sm" : "text-slate-500",
                )}
                onClick={() => setTab("login")}
              >
                登录
              </button>
              <button
                type="button"
                className={clsx(
                  "flex-1 rounded-md py-2 transition",
                  tab === "register" ? "bg-white text-slate-900 shadow-sm" : "text-slate-500",
                )}
                onClick={() => setTab("register")}
              >
                注册
              </button>
            </div>

            <form className="space-y-4" onSubmit={onSubmit}>
              {errMsg ? (
                <p className="rounded-lg bg-red-50 px-3 py-2 text-xs text-red-700" role="alert">
                  {errMsg}
                </p>
              ) : null}
              <div>
                <label htmlFor="login-email" className="mb-1 block text-xs font-medium text-slate-600">邮箱</label>
                <input
                  id="login-email"
                  type="email"
                  required
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  autoComplete="email"
                  className="w-full rounded-lg border border-slate-200 px-3 py-2.5 text-sm outline-none ring-primary focus:border-primary focus:ring-2"
                  placeholder="you@university.edu"
                />
              </div>
              <div>
                <label htmlFor="login-password" className="mb-1 block text-xs font-medium text-slate-600">密码</label>
                <div className="relative">
                  <input
                    id="login-password"
                    type={showPw ? "text" : "password"}
                    required
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                    autoComplete={tab === "login" ? "current-password" : "new-password"}
                    minLength={tab === "register" ? 8 : 1}
                    className="w-full rounded-lg border border-slate-200 py-2.5 pl-3 pr-10 text-sm outline-none ring-primary focus:border-primary focus:ring-2"
                    placeholder="••••••••"
                  />
                  <button
                    type="button"
                    onClick={() => setShowPw((v) => !v)}
                    className="absolute right-2 top-1/2 -translate-y-1/2 rounded p-1 text-slate-400 hover:bg-slate-100 hover:text-slate-600"
                    aria-label={showPw ? "隐藏密码" : "显示密码"}
                  >
                    {showPw ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                  </button>
                </div>
              </div>
              {tab === "login" && (
                <div className="flex justify-end">
                  <button type="button" className="text-xs text-primary hover:underline">
                    忘记密码？
                  </button>
                </div>
              )}
              <button
                type="submit"
                disabled={busy}
                className="w-full rounded-xl bg-primary py-3 text-sm font-semibold text-white hover:bg-primary-hover disabled:opacity-60"
              >
                {busy ? "请稍候…" : tab === "login" ? "登录" : "创建账号"}
              </button>
            </form>

            <div className="mt-8 flex flex-wrap justify-center gap-4 text-xs text-slate-500">
              <a className="hover:text-primary" href="#">
                帮助中心
              </a>
              <a className="hover:text-primary" href="#">
                隐私政策
              </a>
              <a className="hover:text-primary" href="#">
                服务条款
              </a>
            </div>
        </div>
      </section>
    </div>
  );
}
