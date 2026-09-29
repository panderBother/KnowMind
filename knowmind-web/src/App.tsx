import { Navigate, Outlet, Route, Routes } from "react-router-dom";
import { AppShell } from "@/components/layout/AppShell";
import { ChatPage } from "@/pages/ChatPage";
import { DocumentsPage } from "@/pages/DocumentsPage";
import { KnowledgeItemDetailPage } from "@/pages/KnowledgeItemDetailPage";
import { ExpertChatPage } from "@/pages/ExpertChatPage";
import { EvalDashboardPage } from "@/pages/EvalDashboardPage";
import { ExpertsPage } from "@/pages/ExpertsPage";
import { KnowledgeProductionPage } from "@/pages/KnowledgeProductionPage";
import { KnowledgeAnalyticsPage } from "@/pages/KnowledgeAnalyticsPage";
import { KnowledgeBasesPage } from "@/pages/KnowledgeBasesPage";
import { LoginPage } from "@/pages/LoginPage";
import { ReportPage } from "@/pages/ReportPage";
import { ReportsIndexPage } from "@/pages/ReportsIndexPage";
import { SettingsPage } from "@/pages/SettingsPage";
import { ToolsPage } from "@/pages/ToolsPage";
import { getAccessToken } from "@/services/auth";

function RequireAuth() {
  if (!getAccessToken()) {
    return <Navigate to="/login" replace />;
  }
  return <Outlet />;
}

/**
 * 顶层路由：
 * - 登录页独立全屏布局
 * - 其余业务页共用 AppShell（左侧主导航 + 内容区）
 */
export default function App() {
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route element={<RequireAuth />}>
          <Route element={<AppShell />}>
            <Route path="/" element={<Navigate to="/chat" replace />} />
            <Route path="/chat" element={<ChatPage />} />
            <Route path="/knowledge-bases" element={<KnowledgeBasesPage />} />
            <Route path="/knowledge-bases/:kbId/analytics" element={<KnowledgeAnalyticsPage />} />
            <Route path="/knowledge-bases/:kbId/production" element={<KnowledgeProductionPage />} />
            <Route path="/production" element={<KnowledgeProductionPage />} />
            <Route path="/documents" element={<DocumentsPage />} />
            <Route path="/documents/items/:kbId/:itemId" element={<KnowledgeItemDetailPage />} />
            <Route path="/reports" element={<ReportsIndexPage />} />
            <Route path="/reports/:id" element={<ReportPage />} />
            <Route path="/experts" element={<ExpertsPage />} />
            <Route path="/experts/:expertId" element={<ExpertChatPage />} />
            <Route path="/tools" element={<ToolsPage />} />
            <Route path="/quality" element={<EvalDashboardPage />} />
            <Route path="/settings" element={<SettingsPage />} />
          </Route>
        </Route>
        <Route path="*" element={<Navigate to="/chat" replace />} />
      </Routes>
    </div>
  );
}
