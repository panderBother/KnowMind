import { useCallback, useEffect, useState } from "react";
import { ChevronRight, Loader2, LogOut, User, Wrench } from "lucide-react";
import { Link, useNavigate } from "react-router-dom";

import {
  authChangePassword,
  authLogout,
  authMe,
  clearAccessToken,
  type UserPublic,
} from "@/services/auth";

/**
 * 我的 / 设置：账户信息、改密与扩展能力入口。
 */
export function SettingsPage() {
  const navigate = useNavigate();
  const [user, setUser] = useState<UserPublic | null>(null);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [pwdMsg, setPwdMsg] = useState<string | null>(null);
  const [currentPwd, setCurrentPwd] = useState("");
  const [newPwd, setNewPwd] = useState("");
  const [savingPwd, setSavingPwd] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setErr(null);
    try {
      setUser(await authMe());
    } catch (e) {
      setErr(e instanceof Error ? e.message : "加载失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const onChangePassword = async () => {
    if (!currentPwd || !newPwd) return;
    setSavingPwd(true);
    setPwdMsg(null);
    try {
      await authChangePassword(currentPwd, newPwd);
      clearAccessToken();
      setCurrentPwd("");
      setNewPwd("");
      navigate("/login", { replace: true });
    } catch (e) {
      setPwdMsg(e instanceof Error ? e.message : "修改失败");
    } finally {
      setSavingPwd(false);
    }
  };

  return (
    <div className="min-h-0 flex-1 overflow-y-auto p-4 pb-6 lg:p-8">
      <h1 className="text-lg font-semibold text-slate-900 lg:text-xl">我的</h1>
      <p className="mt-1 text-xs text-slate-500 lg:text-sm">账户、模型与扩展能力入口</p>

      {loading ? (
        <p className="mt-6 flex items-center gap-2 text-sm text-slate-500">
          <Loader2 className="h-4 w-4 animate-spin" /> 加载账户信息…
        </p>
      ) : null}
      {err ? <p className="mt-4 text-sm text-red-600">{err}</p> : null}

      {user ? (
        <div className="mt-4 max-w-lg rounded-2xl border border-slate-200 bg-white p-4 shadow-card lg:rounded-xl lg:p-6">
          <div className="flex items-center gap-3">
            <span className="flex h-10 w-10 items-center justify-center rounded-xl bg-primary-soft text-primary">
              <User className="h-5 w-5" />
            </span>
            <div>
              <p className="text-sm font-semibold text-slate-900">{user.email}</p>
              <p className="text-xs text-slate-500">
                注册于 {new Date(user.created_at).toLocaleString()}
              </p>
            </div>
          </div>
        </div>
      ) : null}

      <div className="mt-4 max-w-lg rounded-2xl border border-slate-200 bg-white p-4 shadow-card lg:rounded-xl lg:p-6">
        <h2 className="text-sm font-semibold text-slate-900">修改密码</h2>
        <div className="mt-3 space-y-2">
          <input
            type="password"
            value={currentPwd}
            onChange={(e) => setCurrentPwd(e.target.value)}
            placeholder="当前密码"
            className="w-full rounded-lg border border-slate-200 px-3 py-2 text-sm"
          />
          <input
            type="password"
            value={newPwd}
            onChange={(e) => setNewPwd(e.target.value)}
            placeholder="新密码（至少 8 位）"
            className="w-full rounded-lg border border-slate-200 px-3 py-2 text-sm"
          />
          <button
            type="button"
            disabled={savingPwd || !currentPwd || newPwd.length < 8}
            onClick={() => void onChangePassword()}
            className="rounded-lg bg-primary px-4 py-2 text-sm font-medium text-white disabled:opacity-50"
          >
            {savingPwd ? "保存中…" : "保存新密码"}
          </button>
          {pwdMsg ? <p className="text-xs text-slate-600">{pwdMsg}</p> : null}
        </div>
      </div>

      <ul className="mt-4 max-w-lg space-y-2">
        <li>
          <Link
            to="/tools"
            className="flex items-center justify-between rounded-2xl border border-slate-200 bg-white px-4 py-3.5 text-sm font-medium text-slate-800 shadow-card active:bg-slate-50 lg:rounded-xl"
          >
            <span className="flex items-center gap-3">
              <span className="flex h-9 w-9 items-center justify-center rounded-xl bg-slate-100 text-slate-700">
                <Wrench className="h-5 w-5" />
              </span>
              工具与 MCP
            </span>
            <ChevronRight className="h-4 w-4 text-slate-400" />
          </Link>
        </li>
        <li>
          <button
            type="button"
            onClick={() => void authLogout().finally(() => navigate("/login", { replace: true }))}
            className="flex w-full items-center justify-between rounded-2xl border border-red-100 bg-white px-4 py-3.5 text-sm font-medium text-red-700 shadow-card active:bg-red-50 lg:rounded-xl"
          >
            <span className="flex items-center gap-3">
              <span className="flex h-9 w-9 items-center justify-center rounded-xl bg-red-50 text-red-700">
                <LogOut className="h-5 w-5" />
              </span>
              退出登录并撤销令牌
            </span>
            <ChevronRight className="h-4 w-4 text-red-300" />
          </button>
        </li>
      </ul>
    </div>
  );
}
