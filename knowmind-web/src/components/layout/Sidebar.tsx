import clsx from "clsx";
import {
  BookOpen,
  Bot,
  FileText,
  LayoutDashboard,
  LogOut,
  MessageSquare,
  Settings,
  Sparkles,
  Wrench,
} from "lucide-react";
import { NavLink, useLocation, useNavigate } from "react-router-dom";

import { authLogout } from "@/services/auth";

function isProductionPath(pathname: string): boolean {
  return pathname === "/production" || /\/knowledge-bases\/[^/]+\/production$/.test(pathname);
}

/** 主导航项：与 PRD 侧栏信息架构一致，便于后续权限裁剪 */
const NAV = [
  { to: "/chat", label: "智能对话", icon: MessageSquare },
  { to: "/knowledge-bases", label: "知识库", icon: BookOpen },
  { to: "/documents", label: "文档管理", icon: FileText },
  { to: "/production", label: "知识生产", icon: Sparkles, matchProduction: true },
  { to: "/reports", label: "报告", icon: LayoutDashboard },
  { to: "/experts", label: "专家", icon: Bot },
  { to: "/tools", label: "工具", icon: Wrench },
  { to: "/settings", label: "设置", icon: Settings },
] as const;

export function Sidebar() {
  const location = useLocation();
  const navigate = useNavigate();

  return (
    <aside className="hidden w-56 shrink-0 flex-col border-r border-slate-200 bg-white lg:flex">
      <div className="flex items-center gap-2 border-b border-slate-100 px-4 py-4">
        <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-primary text-sm font-bold text-white">
          K
        </div>
        <div>
          <div className="text-sm font-semibold text-slate-900">KnowMind</div>
          <div className="text-xs text-slate-500">AI 知识助理</div>
        </div>
      </div>
      <nav className="flex flex-1 flex-col gap-0.5 p-2">
        {NAV.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            className={({ isActive }) =>
              clsx(
                "flex items-center gap-2 rounded-lg px-3 py-2 text-sm font-medium transition-colors",
                (isActive || ("matchProduction" in item && item.matchProduction && isProductionPath(location.pathname)))
                  ? "bg-primary-soft text-primary"
                  : "text-slate-600 hover:bg-slate-50 hover:text-slate-900",
              )
            }
          >
            <item.icon className="h-4 w-4 shrink-0" />
            {item.label}
          </NavLink>
        ))}
      </nav>
      <div className="border-t border-slate-100 p-2">
        <button
          type="button"
          onClick={() => void authLogout().finally(() => navigate("/login", { replace: true }))}
          className="flex w-full items-center gap-2 rounded-lg px-3 py-2 text-sm font-medium text-slate-500 transition hover:bg-red-50 hover:text-red-700 active:translate-y-px"
        >
          <LogOut className="h-4 w-4" />
          退出登录
        </button>
      </div>
    </aside>
  );
}
