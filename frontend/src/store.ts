import { create } from "zustand";
import { api } from "./api";
import { soundFx } from "./audio";
import {
  ApproveDispatchResponse,
  CasesSummary,
  DashboardMetrics,
  KycCase,
  LlmHealthStatus,
  ViewId,
} from "./types";

export interface WorkflowState {
  isRunning: boolean;
  activeNode: "idle" | "orchestrator" | "drafter" | "critic" | "complete" | "error";
  streamedText: string;
  streamedSubject: string;
  progress: number;
  attempt: number;
  isRetry: boolean;
  statusLabel: string;
  rejectedReason: string | null;
  logs: string[];
}

const initialWorkflow: WorkflowState = {
  isRunning: false,
  activeNode: "idle",
  streamedText: "",
  streamedSubject: "",
  progress: 0,
  attempt: 0,
  isRetry: false,
  statusLabel: "Awaiting Orchestrator Launch",
  rejectedReason: null,
  logs: [],
};

let activeStream: EventSource | null = null;

interface ChaseZeroStore {
  activeView: ViewId;
  summary: CasesSummary | null;
  selectedCaseId: number | null;
  selectedCase: KycCase | null;
  dashboardMetrics: DashboardMetrics | null;
  searchQuery: string;
  filterTab: string;
  audioEnabled: boolean;
  simulateCriticRejection: boolean;
  loadingCases: boolean;
  loadingCaseDetail: boolean;
  loadingMetrics: boolean;
  error: string | null;
  workflow: WorkflowState;
  lastDispatchResult: ApproveDispatchResponse | null;
  llmStatus: LlmHealthStatus | null;

  setActiveView: (view: ViewId) => void;
  setSearchQuery: (q: string) => void;
  setFilterTab: (tab: string) => void;
  toggleAudio: () => void;
  setSimulateCriticRejection: (val: boolean) => void;
  fetchCases: () => Promise<void>;
  selectCase: (caseId: number, navigateTo?: ViewId) => Promise<void>;
  fetchDashboardMetrics: () => Promise<void>;
  toggleDocumentStatus: (caseId: number, docId: number) => Promise<void>;
  resetCaseDemo: (caseId: number) => Promise<void>;
  updateCaseNote: (caseId: number, note: string) => Promise<void>;
  startLangGraphWorkflow: (
    caseId: number,
    opts?: {
      navigateToDrafting?: boolean;
      simulateRejection?: boolean;
      tone?: string;
    }
  ) => void;
  rescanCritic: (
    caseId: number,
    draftBody?: string,
    simulateFailure?: boolean
  ) => Promise<void>;
  adjustTone: (caseId: number, tone: string) => Promise<void>;
  approveAndSend: (
    caseId: number,
    payload: {
      subject: string;
      body: string;
      notify_analyst: boolean;
      cc_emails: string[];
    }
  ) => Promise<ApproveDispatchResponse>;
  clearLastDispatch: () => void;
  fetchLlmStatus: () => Promise<void>;
  reloadEnvStatus: () => Promise<void>;
}

export const useChaseZeroStore = create<ChaseZeroStore>((set, get) => ({
  activeView: "orchestrator-inbox",
  summary: null,
  selectedCaseId: 1,
  selectedCase: null,
  dashboardMetrics: null,
  searchQuery: "",
  filterTab: "all",
  audioEnabled: true,
  simulateCriticRejection: false,
  loadingCases: false,
  loadingCaseDetail: false,
  loadingMetrics: false,
  error: null,
  workflow: initialWorkflow,
  lastDispatchResult: null,
  llmStatus: null,

  setActiveView: (view) => {
    soundFx.playNodePing(get().audioEnabled);
    set({ activeView: view });
    if (view === "executive-success-dashboard") {
      get().fetchDashboardMetrics();
    }
  },
  setSearchQuery: (q) => {
    set({ searchQuery: q });
    get().fetchCases();
  },
  setFilterTab: (tab) => {
    set({ filterTab: tab });
    get().fetchCases();
  },
  toggleAudio: () => {
    const next = !get().audioEnabled;
    set({ audioEnabled: next });
    if (next) soundFx.playNodePing(true);
  },
  setSimulateCriticRejection: (val) => set({ simulateCriticRejection: val }),

  fetchCases: async () => {
    set({ loadingCases: true, error: null });
    try {
      const { searchQuery, filterTab, selectedCaseId } = get();
      const summary = await api.getCases(searchQuery, filterTab);
      set({ summary, loadingCases: false });
      const targetId = selectedCaseId ?? summary.cases[0]?.id ?? null;
      if (targetId && !get().selectedCase) {
        await get().selectCase(targetId);
      }
    } catch (err) {
      set({
        loadingCases: false,
        error: err instanceof Error ? err.message : "Failed to load KYC cases",
      });
    }
  },

  selectCase: async (caseId, navigateTo) => {
    set({ loadingCaseDetail: true, selectedCaseId: caseId, error: null });
    if (navigateTo) {
      soundFx.playNodePing(get().audioEnabled);
      set({ activeView: navigateTo });
    }
    try {
      const detail = await api.getCaseDetail(caseId);
      set({ selectedCase: detail, loadingCaseDetail: false });
    } catch (err) {
      set({
        loadingCaseDetail: false,
        error: err instanceof Error ? err.message : "Failed to load case details",
      });
    }
  },

  fetchDashboardMetrics: async () => {
    set({ loadingMetrics: true });
    try {
      const metrics = await api.getDashboardMetrics();
      set({ dashboardMetrics: metrics, loadingMetrics: false });
    } catch (err) {
      set({
        loadingMetrics: false,
        error: err instanceof Error ? err.message : "Failed to load COO metrics",
      });
    }
  },

  toggleDocumentStatus: async (caseId, docId) => {
    try {
      const updated = await api.toggleDocument(caseId, docId);
      set({ selectedCase: updated });
      get().fetchCases();
      soundFx.playNodePing(get().audioEnabled);
    } catch (err) {
      set({
        error: err instanceof Error ? err.message : "Failed to toggle document",
      });
    }
  },

  resetCaseDemo: async (caseId) => {
    try {
      const updated = await api.resetCaseDemo(caseId);
      set({
        selectedCase: updated,
        workflow: initialWorkflow,
        lastDispatchResult: null,
      });
      get().fetchCases();
      soundFx.playNodePing(get().audioEnabled);
    } catch (err) {
      set({
        error: err instanceof Error ? err.message : "Failed to reset case",
      });
    }
  },

  updateCaseNote: async (caseId, note) => {
    try {
      const updated = await api.updateCaseNote(caseId, note);
      set({ selectedCase: updated });
      soundFx.playNodePing(get().audioEnabled);
    } catch (err) {
      set({
        error: err instanceof Error ? err.message : "Failed to update case note",
      });
    }
  },

  startLangGraphWorkflow: (caseId, opts = {}) => {
    if (activeStream) {
      activeStream.close();
      activeStream = null;
    }
    const simReject = opts.simulateRejection ?? get().simulateCriticRejection;
    const tone = opts.tone ?? "institutional";
    if (opts.navigateToDrafting !== false) {
      set({ activeView: "ai-drafting-agent" });
    }
    soundFx.playNodePing(get().audioEnabled);
    set({
      selectedCaseId: caseId,
      workflow: {
        isRunning: true,
        activeNode: "orchestrator",
        streamedText: "",
        streamedSubject: "",
        progress: 5,
        attempt: 1,
        isRetry: false,
        statusLabel: "Orchestrator Node querying SQLite for SLA breaches...",
        rejectedReason: null,
        logs: ["[LangGraph] Starting StateGraph execution..."],
      },
    });

    const es = api.openWorkflowStream(caseId, simReject, tone);
    activeStream = es;
    es.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        const audio = get().audioEnabled;
        const type = String(data.type ?? "");
        if (type === "node_start") {
          const node = (data.node ?? "orchestrator") as WorkflowState["activeNode"];
          const label = String(data.label ?? "");
          const attempt = Number(data.attempt ?? get().workflow.attempt ?? 1);
          const isRetry = Boolean(data.is_retry);
          soundFx.playNodePing(audio);
          set((s) => ({
            workflow: {
              ...s.workflow,
              activeNode: node,
              attempt,
              isRetry,
              statusLabel: label,
              streamedText: node === "drafter" ? "" : s.workflow.streamedText,
              progress: node === "orchestrator" ? 15 : node === "drafter" ? 40 : 85,
              logs: [...s.workflow.logs, `[${node.toUpperCase()}] ${label}`],
            },
          }));
        } else if (type === "orchestrator_result") {
          const breached = Number(data.breached_count ?? 0);
          const missing = Number(data.missing_docs_count ?? 0);
          set((s) => ({
            workflow: {
              ...s.workflow,
              progress: 28,
              logs: [
                ...s.workflow.logs,
                `[ORCHESTRATOR] Found ${breached} SLA-breached cases (>3d); gathered ${missing} missing docs.`,
              ],
            },
          }));
        } else if (type === "draft_token") {
          const chunk = String(data.chunk ?? "");
          const prog = Number(data.progress ?? 50);
          if (prog % 16 < 5) soundFx.playDraftTick(audio);
          set((s) => ({
            workflow: {
              ...s.workflow,
              streamedText: s.workflow.streamedText + chunk,
              progress: Math.max(40, Math.min(82, 40 + Math.round(prog * 0.4))),
            },
          }));
        } else if (type === "draft_complete") {
          const subject = String(data.subject ?? "");
          const body = String(data.body ?? "");
          set((s) => ({
            workflow: {
              ...s.workflow,
              streamedSubject: subject,
              streamedText: body,
              progress: 84,
              logs: [
                ...s.workflow.logs,
                `[DRAFTER] Completed draft v${data.attempt ?? 1} (${data.word_count ?? 0} words).`,
              ],
            },
          }));
        } else if (type === "critic_result") {
          const passed = Boolean(data.passed);
          const feedback = String(data.feedback ?? "");
          const score = Number(data.score ?? 100);
          if (passed) soundFx.playCriticPass(audio);
          else soundFx.playCriticReject(audio);
          set((s) => ({
            workflow: {
              ...s.workflow,
              rejectedReason: passed ? s.workflow.rejectedReason : feedback,
              statusLabel: passed
                ? `Critic QA Passed (${score}/100) — Status set to Draft_Ready`
                : `Critic QA Rejected Draft v${data.attempt ?? 1} — Routing back to Drafter Node!`,
              logs: [
                ...s.workflow.logs,
                passed
                  ? `[CRITIC] PASSED (${score}/100) — ${feedback}`
                  : `[CRITIC] REJECTED (${score}/100) -> Routing back to Drafter: ${feedback}`,
              ],
            },
          }));
        } else if (type === "workflow_complete") {
          const updatedCase = data.case as KycCase | undefined;
          es.close();
          activeStream = null;
          set((s) => ({
            selectedCase: updatedCase ?? s.selectedCase,
            workflow: {
              ...s.workflow,
              isRunning: false,
              activeNode: "complete",
              progress: 100,
              statusLabel: "Drafting & Compliance QA Complete — Ready for Review",
            },
          }));
          get().fetchCases();
          get().fetchDashboardMetrics();
        } else if (type === "error") {
          es.close();
          activeStream = null;
          set((s) => ({
            error: String(data.message ?? "Workflow stream error"),
            workflow: {
              ...s.workflow,
              isRunning: false,
              activeNode: "error",
              statusLabel: "Workflow Error",
            },
          }));
        }
      } catch {
        // ignore parse issues
      }
    };
    es.onerror = () => {
      es.close();
      activeStream = null;
      set((s) =>
        s.workflow.isRunning
          ? {
              workflow: {
                ...s.workflow,
                isRunning: false,
                activeNode: "error",
                statusLabel: "SSE stream disconnected",
              },
            }
          : s
      );
    };
  },

  rescanCritic: async (caseId, draftBody, simulateFailure = false) => {
    try {
      const updated = await api.rescanCritic(caseId, draftBody, simulateFailure);
      set({ selectedCase: updated });
      if (updated.critic_passed) soundFx.playCriticPass(get().audioEnabled);
      else soundFx.playCriticReject(get().audioEnabled);
      get().fetchDashboardMetrics();
    } catch (err) {
      set({
        error: err instanceof Error ? err.message : "Critic scan failed",
      });
    }
  },

  adjustTone: async (caseId, tone) => {
    try {
      const updated = await api.adjustTone(caseId, tone);
      set({ selectedCase: updated });
      soundFx.playNodePing(get().audioEnabled);
    } catch (err) {
      set({
        error: err instanceof Error ? err.message : "Tone adjustment failed",
      });
    }
  },

  approveAndSend: async (caseId, payload) => {
    const res = await api.approveAndSend(caseId, payload);
    soundFx.playDispatchSuccess(get().audioEnabled);
    set({ selectedCase: res.case, lastDispatchResult: res });
    get().fetchCases();
    get().fetchDashboardMetrics();
    return res;
  },

  clearLastDispatch: () => set({ lastDispatchResult: null }),

  fetchLlmStatus: async () => {
    try {
      const status = await api.getHealth();
      set({ llmStatus: status });
    } catch {
      // ignore
    }
  },

  reloadEnvStatus: async () => {
    try {
      const status = await api.reloadEnv();
      set({ llmStatus: status });
      soundFx.playNodePing(get().audioEnabled);
    } catch (err) {
      set({
        error: err instanceof Error ? err.message : "Failed to reload .env",
      });
    }
  },
}));
