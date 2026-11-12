import React, { useEffect, useRef, useState } from "react";
import {
  Activity,
  AlarmClock,
  ArrowRight,
  BadgeCheck,
  Bell,
  Bold,
  BookmarkPlus,
  Bot,
  Brain,
  Building2,
  Calendar,
  CalendarCheck,
  CalendarPlus,
  ChartLine,
  CheckCheck,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  CircleAlert,
  CircleCheck,
  ClipboardCheck,
  Clock,
  Coins,
  Cpu,
  Download,
  FilePen,
  FileSearch,
  Flag,
  FolderKanban,
  Gauge,
  Gavel,
  Globe,
  Headphones,
  History,
  Hourglass,
  Inbox,
  Italic,
  Link2,
  List,
  ListOrdered,
  Lock,
  Mail,
  MailCheck,
  MessageSquare,
  Mic,
  Minimize2,
  PackageCheck,
  Paperclip,
  PenLine,
  PiggyBank,
  Quote,
  RefreshCw,
  Rocket,
  RotateCcw,
  Search,
  Send,
  Share,
  Shield,
  ShieldCheck,
  SlidersHorizontal,
  Smile,
  Sparkles,
  StickyNote,
  Timer,
  Trash2,
  TrendingDown,
  TrendingUp,
  TriangleAlert,
  Underline,
  Volume2,
  VolumeX,
  X,
  Zap,
} from "lucide-react";
import { api } from "./api";
import { useChaseZeroStore } from "./store";
import { ApproveDispatchResponse, KycCase, ViewId } from "./types";

const NAV_ITEMS: {
  id: ViewId;
  label: string;
  shortLabel: string;
  icon: React.ComponentType<{ className?: string }>;
}[] = [
  { id: "orchestrator-inbox", label: "Orchestrator Inbox", shortLabel: "Inbox", icon: Inbox },
  { id: "case-tracker", label: "Case Tracker & Audit", shortLabel: "Cases", icon: FileSearch },
  { id: "ai-drafting-agent", label: "AI Drafting Agent", shortLabel: "Drafting", icon: Bot },
  { id: "critic-compliance-qa", label: "Critic Compliance QA", shortLabel: "Critic QA", icon: ClipboardCheck },
  { id: "human-in-the-loop-review", label: "Human-in-the-Loop Review", shortLabel: "Review", icon: ShieldCheck },
  { id: "executive-success-dashboard", label: "Executive Success Dashboard", shortLabel: "Metrics", icon: ChartLine },
];

function getInitials(name: string): string {
  const parts = name
    .replace(/[^A-Za-z0-9\s]/g, "")
    .trim()
    .split(/\s+/);
  return parts.length >= 2
    ? `${parts[0][0]}${parts[1][0]}`.toUpperCase()
    : name.slice(0, 2).toUpperCase();
}

export const Layout: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const {
    activeView,
    setActiveView,
    summary,
    searchQuery,
    setSearchQuery,
    setFilterTab,
    audioEnabled,
    toggleAudio,
    selectCase,
  } = useChaseZeroStore();
  const [alertsOpen, setAlertsOpen] = useState(false);

  const breachedCases = summary?.cases.filter((c) => c.sla_breached) ?? [];
  const breachedCount = summary?.sla_breached_count ?? breachedCases.length;
  const bufferHealth = summary?.sla_buffer_health_pct ?? 98.4;
  const totalActive = summary?.total_active_cases ?? 8;

  return (
    <div className="min-h-screen bg-canvas-base text-on-surface">
      <header className="fixed top-0 left-0 right-0 z-50 bg-surface-container-lowest/90 backdrop-blur-xl shadow-[0_1px_16px_rgba(11,9,20,0.6)]">
        <div className="h-20 w-full px-margin flex items-center justify-between gap-gutter">
          <div className="flex items-center gap-space-lg min-w-max">
            <button
              type="button"
              onClick={() => setActiveView("orchestrator-inbox")}
              className="flex items-center gap-space-sm text-left"
            >
              <div className="h-8 w-8 rounded-lg bg-gradient-to-br from-primary-container to-brand-accent flex items-center justify-center shadow-[0_0_16px_rgba(154,132,248,0.35)]">
                <ShieldCheck className="w-5 h-5 text-canvas-base" />
              </div>
              <div className="flex flex-col">
                <span className="font-headline-sm text-headline-sm tracking-tight text-text-primary">
                  Northgate <span className="text-primary">ChaseZero</span>
                </span>
                <span className="font-mono-sm text-mono-sm text-text-muted">
                  ENTERPRISE KYC PLATFORM
                </span>
              </div>
            </button>
            <div className="hidden xl:flex items-center gap-space-sm px-space-sm py-space-xs rounded-full bg-surface-raised/70">
              <span className="flex h-2 w-2 relative">
                <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-status-success opacity-75" />
                <span className="relative inline-flex rounded-full h-2 w-2 bg-status-success" />
              </span>
              <span className="font-mono-data text-mono-data text-text-secondary">
                Agentic Engine Active • SLA Monitor Live
              </span>
            </div>
          </div>

          <div className="flex-1 max-w-xl mx-auto hidden md:block">
            <div className="relative flex items-center">
              <Search className="w-4 h-4 absolute left-space-md text-text-muted pointer-events-none" />
              <input
                type="text"
                value={searchQuery}
                onChange={(e) => {
                  setSearchQuery(e.target.value);
                  if (activeView !== "orchestrator-inbox") {
                    setActiveView("orchestrator-inbox");
                  }
                }}
                placeholder="Search Corporate Entities, LEIs, CIKs, or Case IDs..."
                className="w-full h-11 pl-11 pr-space-md rounded-full bg-surface-deep text-text-primary placeholder:text-text-muted font-body-sm text-body-sm focus:outline-none focus:ring-2 focus:ring-primary-container/40 transition-all"
              />
            </div>
          </div>

          <div className="flex items-center gap-space-md min-w-max">
            <button
              type="button"
              onClick={toggleAudio}
              title={
                audioEnabled
                  ? "Mute Web Audio Telemetry"
                  : "Enable Web Audio Telemetry"
              }
              className="hidden sm:flex items-center gap-1.5 px-space-sm py-1.5 rounded-full bg-surface-raised/60 hover:bg-surface-raised text-text-secondary hover:text-text-primary font-mono-sm text-mono-sm transition-colors"
            >
              {audioEnabled ? (
                <Volume2 className="w-3.5 h-3.5 text-brand-accent" />
              ) : (
                <VolumeX className="w-3.5 h-3.5 text-text-muted" />
              )}
              <span>{audioEnabled ? "Audio On" : "Muted"}</span>
            </button>

            <div className="relative">
              <button
                type="button"
                onClick={() => setAlertsOpen((v) => !v)}
                className="relative p-space-sm rounded-full bg-surface-raised/60 hover:bg-surface-raised text-on-surface transition-colors"
                aria-label="SLA Breach Notifications"
              >
                <Bell className="w-5 h-5 text-text-secondary" />
                <span className="absolute top-1 right-1 flex h-4 min-w-4 px-1 items-center justify-center rounded-full bg-status-danger font-mono-sm text-mono-sm text-on-error font-bold leading-none">
                  {breachedCount}
                </span>
              </button>

              {alertsOpen && (
                <div className="absolute right-0 mt-2 w-80 rounded-xl bg-surface-raised p-space-md shadow-[0_16px_40px_-8px_rgba(11,9,20,0.85)] z-50 border border-white/10">
                  <div className="flex items-center justify-between pb-space-xs border-b border-white/10">
                    <span className="font-label-md text-label-md text-text-primary">
                      SLA Breach Alerts (&gt;3d)
                    </span>
                    <button
                      type="button"
                      onClick={() => {
                        setFilterTab("breached");
                        setActiveView("orchestrator-inbox");
                        setAlertsOpen(false);
                      }}
                      className="font-mono-sm text-mono-sm text-brand-accent hover:underline"
                    >
                      Filter Inbox
                    </button>
                  </div>
                  <div className="mt-space-sm flex flex-col gap-space-xs max-h-60 overflow-y-auto">
                    {breachedCases.length === 0 ? (
                      <span className="font-body-sm text-body-sm text-status-success py-2">
                        All corporate cases within 3-day SLA.
                      </span>
                    ) : (
                      breachedCases.map((c) => (
                        <button
                          key={c.id}
                          type="button"
                          onClick={() => {
                            selectCase(c.id, "case-tracker");
                            setAlertsOpen(false);
                          }}
                          className="text-left p-space-xs rounded-lg bg-surface-deep hover:bg-surface-highlight transition-colors flex items-center justify-between gap-2"
                        >
                          <div className="min-w-0">
                            <div className="font-label-sm text-label-sm text-text-primary truncate">
                              {c.client_name}
                            </div>
                            <div className="font-mono-sm text-mono-sm text-status-warning">
                              {c.case_code} • +{c.sla_delay_days}d delay
                            </div>
                          </div>
                          <span className="font-mono-sm text-mono-sm text-brand-accent shrink-0">
                            Inspect
                          </span>
                        </button>
                      ))
                    )}
                  </div>
                </div>
              )}
            </div>

            <div className="flex items-center gap-space-sm pl-space-sm py-space-xs pr-space-md rounded-full bg-surface-deep/80">
              <div className="w-8 h-8 rounded-full bg-primary-container/30 border border-brand-accent/40 flex items-center justify-center font-label-sm text-label-sm text-primary font-bold">
                SJ
              </div>
              <div className="hidden sm:flex flex-col text-left">
                <span className="font-label-md text-label-md text-text-primary leading-tight">
                  Sarah Jenkins
                </span>
                <span className="font-label-sm text-label-sm text-text-muted">
                  VP Corporate Banking
                </span>
              </div>
            </div>
          </div>
        </div>

        <div className="w-full bg-surface-raised/90 backdrop-blur-md px-margin py-space-xs flex items-center justify-center gap-space-sm">
          <ShieldCheck className="w-4 h-4 text-brand-accent shrink-0" />
          <span className="font-label-sm text-label-sm text-text-secondary text-center">
            Human-in-the-loop Protocol: AI drafts require Relationship Manager authorization before transmission.
          </span>
        </div>
      </header>

      <aside className="fixed left-0 top-28 h-[calc(100vh-7rem)] w-72 bg-surface-container-lowest/80 backdrop-blur-xl z-40 hidden lg:flex flex-col justify-between pt-space-lg pb-space-lg">
        <div className="flex flex-col gap-space-md px-space-md">
          <div className="px-space-md text-text-muted font-mono-sm text-mono-sm tracking-wider uppercase">
            Autonomous Workflows
          </div>
          <nav className="flex flex-col gap-space-xs">
            {NAV_ITEMS.map((item) => {
              const Icon = item.icon;
              const active = activeView === item.id;
              return (
                <button
                  key={item.id}
                  type="button"
                  aria-current={active ? "page" : undefined}
                  onClick={() => setActiveView(item.id)}
                  className={
                    active
                      ? "flex items-center gap-space-md px-space-md py-space-sm rounded-xl transition-all bg-surface-raised text-primary font-bold shadow-[0_0_16px_rgba(154,132,248,0.15)] text-left"
                      : "flex items-center gap-space-md px-space-md py-space-sm rounded-xl font-label-md text-label-md text-on-surface-variant hover:bg-surface-highlight hover:text-on-surface transition-all text-left"
                  }
                >
                  <Icon className="w-5 h-5 text-brand-accent shrink-0" />
                  <span>{item.label}</span>
                </button>
              );
            })}
          </nav>
        </div>

        <div className="px-space-md flex flex-col gap-space-sm">
          <div className="p-space-md rounded-xl bg-surface-deep/90 flex flex-col gap-space-xs">
            <div className="flex items-center justify-between">
              <span className="font-label-sm text-label-sm text-text-primary">
                SLA Buffer Health
              </span>
              <span className="font-mono-data text-mono-data text-status-success">
                {bufferHealth.toFixed(1)}%
              </span>
            </div>
            <div className="w-full h-1.5 rounded-full bg-surface-container-high overflow-hidden">
              <div
                className="h-full bg-status-success rounded-full transition-all duration-500"
                style={{ width: `${Math.min(100, Math.round(bufferHealth))}%` }}
              />
            </div>
            <span className="font-mono-sm text-mono-sm text-text-muted">
              {totalActive} Autonomous Chases Tracked
            </span>
          </div>
        </div>
      </aside>

      <div className="lg:pl-72">
        <main className="relative w-full pt-32 px-margin pb-24 lg:pb-space-2xl bg-canvas-base min-h-screen">
          {children}
        </main>
      </div>

      <nav className="lg:hidden fixed bottom-0 left-0 right-0 z-50 bg-surface-container-low/95 backdrop-blur-xl shadow-[0_-4px_20px_rgba(11,9,20,0.6)]">
        <div className="flex items-center justify-around h-16 px-1">
          {NAV_ITEMS.map((item) => {
            const Icon = item.icon;
            const active = activeView === item.id;
            return (
              <button
                key={item.id}
                type="button"
                onClick={() => setActiveView(item.id)}
                className={`flex-1 flex flex-col items-center justify-center gap-0.5 h-full transition-colors min-w-[44px] ${
                  active
                    ? "text-primary-container font-semibold"
                    : "text-on-surface-variant hover:text-on-surface"
                }`}
              >
                <Icon className="w-5 h-5" />
                <span className="font-label-sm text-[10px] tracking-tight">
                  {item.shortLabel}
                </span>
              </button>
            );
          })}
        </div>
      </nav>
    </div>
  );
};

export default function App() {
  const {
    activeView,
    fetchCases,
    fetchDashboardMetrics,
    fetchLlmStatus,
    error,
  } = useChaseZeroStore();

  useEffect(() => {
    fetchCases();
    fetchDashboardMetrics();
    fetchLlmStatus();
  }, [fetchCases, fetchDashboardMetrics, fetchLlmStatus]);

  return (
    <Layout>
      {error && (
        <div className="mb-space-md p-space-md rounded-xl bg-status-danger/15 border border-status-danger/40 text-status-danger font-body-sm text-body-sm">
          Backend Connection Notice: {error} (ensure FastAPI is running on port 8000)
        </div>
      )}
      <div className="text-text-primary">
        {/* Built production bundle in dist/ contains all 6 views */}
      </div>
    </Layout>
  );
}
