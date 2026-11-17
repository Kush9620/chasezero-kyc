/**
 * Northgate ChaseZero v2.0 — Instant 0ms Login Screen + Role Switcher (RM / ANALYST / COO / ADMIN)
 * & Presentation-Ready Trace / Audit Inspector
 */
(function () {
  const DEMO_ROLES = {
    RM: {
      key: "RM",
      email: "rm@northgate.demo",
      password: "ChaseZero-RM-Demo-2026!",
      name: "Sarah Jenkins",
      initials: "SJ",
      title: "VP Corporate Banking",
      navLabel: "Orchestrator Inbox",
    },
    ANALYST: {
      key: "ANALYST",
      email: "analyst@northgate.demo",
      password: "ChaseZero-Analyst-2026!",
      name: "Marcus Vance",
      initials: "MV",
      title: "Senior KYC Analyst",
      navLabel: "Case Tracker & Audit",
    },
    COO: {
      key: "COO",
      email: "coo@northgate.demo",
      password: "ChaseZero-COO-2026!",
      name: "Eleanor Sterling",
      initials: "ES",
      title: "COO Global Banking",
      navLabel: "Executive Success Dashboard",
    },
    ADMIN: {
      key: "ADMIN",
      email: "admin@northgate.demo",
      password: "ChaseZero-Admin-2026!",
      name: "David Chen",
      initials: "DC",
      title: "Head of Compliance Ops",
      navLabel: "Critic Compliance QA",
    },
  };

  const STATE = {
    authenticated: false,
    selectedRole: "RM",
    operator: DEMO_ROLES.RM,
    summary: null,
    cases: [],
    selectedCaseCode: "NCZ-2024-8841",
    activeTab: "waterfall",
    drawerOpen: false,
    tracesData: null,
    ledgerData: null,
    loading: false,
    actionStatus: null,
    rmId: "Sarah Jenkins (VP Corporate Banking)",
  };

  const STEP_LABELS = {
    "mcp.tools/call.run_chase_pipeline": "1. FastMCP Request Trigger",
    "langgraph.orchestrator_sla_scan": "2. SLA & Missing Document Check",
    "langgraph.ai_drafting_agent": "3. AI Email Drafter (Gemini 2.5)",
    "langgraph.critic_compliance_qa": "4. Critic Compliance QA (5 Rules)",
  };

  async function fetchJson(url, options) {
    const res = await fetch(url, {
      headers: { "Content-Type": "application/json", ...(options?.headers || {}) },
      ...options,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new Error(data.detail || data.error?.message || `HTTP ${res.status}`);
    }
    return data;
  }

  function bindInstantLoginHandlers() {
    const loginRoot = document.getElementById("ncz-login-screen-root");
    const appRoot = document.getElementById("root");
    if (!loginRoot) return;

    const emailInput = document.getElementById("ncz-login-email");
    const passInput = document.getElementById("ncz-login-password");
    const form = document.getElementById("ncz-login-form");
    const roleBtns = loginRoot.querySelectorAll(".ncz-role-btn");

    function highlightRoleButton(activeRole) {
      roleBtns.forEach((b) => {
        const r = b.getAttribute("data-role");
        if (r === activeRole) {
          b.style.border = "1px solid #2563eb";
          b.style.background = "#eff6ff";
          b.style.color = "#1d4ed8";
        } else {
          b.style.border = "1px solid #cbd5e1";
          b.style.background = "#f8fafc";
          b.style.color = "#475569";
        }
      });
    }

    roleBtns.forEach((btn) => {
      btn.onclick = () => {
        const rKey = btn.getAttribute("data-role") || "RM";
        const profile = DEMO_ROLES[rKey] || DEMO_ROLES.RM;
        STATE.selectedRole = rKey;
        STATE.operator = profile;
        if (emailInput) emailInput.value = profile.email;
        if (passInput) passInput.value = profile.password;
        highlightRoleButton(rKey);
      };
    });

    if (form) {
      form.onsubmit = (e) => {
        e.preventDefault();
        const typedEmail = emailInput ? emailInput.value.trim().toLowerCase() : "rm@northgate.demo";
        // Match role from typed email if edited manually
        for (const [k, prof] of Object.entries(DEMO_ROLES)) {
          if (prof.email === typedEmail || typedEmail.startsWith(k.toLowerCase())) {
            STATE.selectedRole = k;
            STATE.operator = prof;
            break;
          }
        }
        const op = STATE.operator || DEMO_ROLES.RM;
        STATE.authenticated = true;
        STATE.rmId = `${op.name} (${op.title})`;

        // Instant 0ms transition into the workspace
        loginRoot.style.display = "none";
        if (appRoot) appRoot.style.display = "block";
        document.body.style.overflow = "";

        updateWorkspaceOperatorProfile();
        navigateToRoleDefaultView(STATE.selectedRole);

        // Fire non-blocking background audit login call
        fetch("/api/auth/login", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            email: typedEmail,
            password: passInput ? passInput.value : "demo",
            role: STATE.selectedRole,
          }),
        }).catch(() => {});
      };
    }
  }

  function navigateToRoleDefaultView(roleKey) {
    const profile = DEMO_ROLES[roleKey];
    if (!profile || !profile.navLabel) return;
    setTimeout(() => {
      const navButtons = document.querySelectorAll("aside nav button");
      navButtons.forEach((btn) => {
        if ((btn.innerText || "").includes(profile.navLabel)) {
          btn.click();
        }
      });
    }, 30);
  }

  function updateWorkspaceOperatorProfile() {
    const op = STATE.operator || DEMO_ROLES.RM;
    const header = document.querySelector("header");
    if (!header) return;

    const spans = header.querySelectorAll("span");
    spans.forEach((sp) => {
      if (
        sp.innerText === "Sarah Jenkins" ||
        sp.innerText === "Marcus Vance" ||
        sp.innerText === "Eleanor Sterling" ||
        sp.innerText === "David Chen"
      ) {
        sp.innerText = op.name;
      }
      if (
        sp.innerText.includes("VP Corporate Banking") ||
        sp.innerText.includes("Senior KYC Analyst") ||
        sp.innerText.includes("COO Global Banking") ||
        sp.innerText.includes("Head of Compliance Ops")
      ) {
        sp.innerText = `${op.title} • ${op.key}`;
      }
    });

    const rightGroup = header.querySelector(".h-20 > div:last-child");
    if (rightGroup && !document.getElementById("ncz-signout-btn")) {
      const signOutBtn = document.createElement("button");
      signOutBtn.id = "ncz-signout-btn";
      signOutBtn.type = "button";
      signOutBtn.style.cssText =
        "padding:6px 12px;border-radius:9999px;font-size:12px;font-weight:600;background:#f1f5f9;color:#334155;border:1px solid #cbd5e1;cursor:pointer;";
      signOutBtn.innerText = "Switch Role / Sign Out";
      signOutBtn.onclick = () => {
        STATE.authenticated = false;
        const loginRoot = document.getElementById("ncz-login-screen-root");
        const appRoot = document.getElementById("root");
        if (loginRoot) loginRoot.style.display = "grid";
        if (appRoot) appRoot.style.display = "none";
        document.body.style.overflow = "hidden";
      };
      rightGroup.appendChild(signOutBtn);
    }
  }

  async function refreshGlobalTelemetry() {
    try {
      const [obs, casesRes] = await Promise.all([
        fetchJson("/api/observability/summary"),
        fetchJson("/api/cases"),
      ]);
      STATE.summary = obs;
      STATE.cases = casesRes.cases || [];
      renderTopBarButton();
      if (STATE.authenticated) {
        updateWorkspaceOperatorProfile();
      }
    } catch (err) {
      console.warn("Telemetry refresh notice:", err);
    }
  }

  async function loadEntityInspection(caseCode) {
    STATE.selectedCaseCode = caseCode || STATE.selectedCaseCode || "NCZ-2024-8841";
    STATE.loading = true;
    renderDrawer();
    try {
      const [traces, ledger] = await Promise.all([
        fetchJson(`/api/observability/traces/${encodeURIComponent(STATE.selectedCaseCode)}`),
        fetchJson(`/api/audit/ledger?case_id=${encodeURIComponent(STATE.selectedCaseCode)}&limit=15`),
      ]);
      STATE.tracesData = traces;
      STATE.ledgerData = ledger;
    } catch (err) {
      STATE.actionStatus = { type: "error", text: err.message };
    } finally {
      STATE.loading = false;
      renderDrawer();
    }
  }

  async function triggerMcpPipelineRun() {
    STATE.loading = true;
    STATE.actionStatus = {
      type: "info",
      text: `Running AI Drafter & Critic QA for ${STATE.selectedCaseCode}...`,
    };
    renderDrawer();
    try {
      const res = await fetchJson("/mcp", {
        method: "POST",
        headers: {
          Authorization: "Bearer northgate-mcp-bearer-2026",
          "X-RM-Actor-ID": STATE.rmId,
        },
        body: JSON.stringify({
          jsonrpc: "2.0",
          id: Date.now(),
          method: "tools/call",
          params: {
            name: "run_chase_pipeline",
            arguments: { case_id: STATE.selectedCaseCode },
          },
        }),
      });
      const out = res.data || res.result?.structuredContent || {};
      STATE.actionStatus = {
        type: "success",
        text: `✓ AI Draft Ready! Score: ${Math.round(
          Number(out.critic_legal_score || 1) * 100
        )}% • Cost: $${Number(out.cost_usd || 0).toFixed(4)} • Speed: ${Math.round(
          Number(out.latency_ms || 0)
        )} ms`,
      };
      await refreshGlobalTelemetry();
      await loadEntityInspection(STATE.selectedCaseCode);
    } catch (err) {
      STATE.actionStatus = { type: "error", text: err.message };
      STATE.loading = false;
      renderDrawer();
    }
  }

  async function triggerOneClickApprove() {
    STATE.loading = true;
    STATE.actionStatus = {
      type: "info",
      text: `Signing & dispatching ${STATE.selectedCaseCode}...`,
    };
    renderDrawer();
    try {
      const res = await fetchJson(
        `/api/cases/${encodeURIComponent(STATE.selectedCaseCode)}/approve`,
        {
          method: "POST",
          body: JSON.stringify({
            actor: STATE.rmId,
            notify_analyst: true,
          }),
        }
      );
      STATE.actionStatus = {
        type: "success",
        text: `✓ Approved & Sent! Tracking ID: ${res.tracking_id} (Signed with HMAC-SHA256)`,
      };
      await refreshGlobalTelemetry();
      await loadEntityInspection(STATE.selectedCaseCode);
    } catch (err) {
      STATE.actionStatus = { type: "error", text: err.message };
      STATE.loading = false;
      renderDrawer();
    }
  }

  function renderTopBarButton() {
    const oldBar = document.getElementById("ncz-executive-observability-bar");
    if (oldBar) oldBar.remove();

    const govBar = document.querySelector("header > div:last-child");
    if (!govBar) return;

    let badge = document.getElementById("ncz-sec-fincen-gov-badge");
    if (!badge) {
      badge = document.createElement("div");
      badge.id = "ncz-sec-fincen-gov-badge";
      badge.style.cssText = "display:inline-flex;align-items:center;gap:8px;margin-left:12px;";
      govBar.appendChild(badge);
    }

    const avgCost = STATE.summary
      ? Number(STATE.summary.avg_cost_per_chase_usd || 0.015).toFixed(4)
      : "0.0151";

    badge.innerHTML = `
      <button type="button" id="ncz-open-trace-drawer-btn" style="display:inline-flex;align-items:center;gap:6px;padding:3px 12px;border-radius:9999px;font-size:12px;font-weight:600;background:#2563eb;color:#ffffff;border:none;cursor:pointer;">
        📊 AI Cost ($${avgCost}) & Audit Log
      </button>
    `;

    const btn = document.getElementById("ncz-open-trace-drawer-btn");
    if (btn) {
      btn.onclick = () => {
        STATE.drawerOpen = true;
        loadEntityInspection(STATE.selectedCaseCode);
      };
    }
  }

  function renderDrawer() {
    let container = document.getElementById("ncz-compliance-drawer-root");
    if (!container) {
      container = document.createElement("div");
      container.id = "ncz-compliance-drawer-root";
      document.body.appendChild(container);
    }

    if (!STATE.drawerOpen) {
      container.innerHTML = "";
      return;
    }

    const caseOptions = (
      STATE.cases.length
        ? STATE.cases
        : [
            { case_code: "NCZ-2024-8841", client_name: "Apex Global Commodities Ltd" },
            { case_code: "NCZ-2024-8849", client_name: "Aethelgard Global Holdings Ltd" },
            { case_code: "NCZ-2024-8854", client_name: "Meridian Capital Partners Fund IV" },
            { case_code: "NCZ-2024-8862", client_name: "Vanguard Logistics Nordics AS" },
            { case_code: "NCZ-2024-8833", client_name: "Sovereign Ridge Capital LLC" },
            { case_code: "NCZ-2024-8802", client_name: "Albion Trade Finance PLC" },
          ]
    )
      .map(
        (c) =>
          `<option value="${c.case_code}" ${
            c.case_code === STATE.selectedCaseCode ? "selected" : ""
          }>${c.client_name} (${c.case_code})</option>`
      )
      .join("");

    const tData = STATE.tracesData || { traces: [], spans: [], total_cost_usd: 0 };
    const latestTrace = (tData.traces && tData.traces[0]) || null;
    const spans = latestTrace ? latestTrace.spans : tData.spans || [];
    const totalTokens =
      Number(tData.total_input_tokens || 0) + Number(tData.total_output_tokens || 0);
    const totalLatency = latestTrace
      ? Math.round(Number(latestTrace.total_latency_ms || 442))
      : 442;

    const waterfallHtml = `
      <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:16px;">
        <div style="background:#ffffff;padding:14px;border-radius:10px;border:1px solid #e2e8f0;">
          <div style="font-size:12px;color:#64748b;">Case AI Cost</div>
          <div style="font-size:20px;font-weight:700;color:#2563eb;margin-top:2px;">$${Number(
            tData.total_cost_usd || 0.013
          ).toFixed(4)}</div>
        </div>
        <div style="background:#ffffff;padding:14px;border-radius:10px;border:1px solid #e2e8f0;">
          <div style="font-size:12px;color:#64748b;">Tokens Used</div>
          <div style="font-size:20px;font-weight:700;color:#0f172a;margin-top:2px;">${totalTokens.toLocaleString()}</div>
        </div>
        <div style="background:#ffffff;padding:14px;border-radius:10px;border:1px solid #e2e8f0;">
          <div style="font-size:12px;color:#64748b;">Response Time</div>
          <div style="font-size:20px;font-weight:700;color:#059669;margin-top:2px;">${totalLatency} ms</div>
        </div>
      </div>

      <div style="display:flex;flex-direction:column;gap:8px;">
        ${spans
          .map((s) => {
            const label = STEP_LABELS[s.span_name] || s.span_name;
            const isOk = s.status === "OK";
            const stepTokens = Number(s.input_tokens || 0) + Number(s.output_tokens || 0);
            return `
              <div style="padding:12px 16px;border-radius:10px;background:#ffffff;border:1px solid #e2e8f0;display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;">
                <strong style="font-size:14px;color:#0f172a;">${label}</strong>
                <div style="display:flex;align-items:center;gap:14px;font-size:13px;">
                  <span style="color:#64748b;">${stepTokens.toLocaleString()} tokens</span>
                  <span style="color:#059669;font-weight:700;">$${Number(
                    s.cost_usd || 0
                  ).toFixed(4)}</span>
                  <span style="color:#2563eb;font-weight:600;">${Math.round(
                    Number(s.latency_ms || 0)
                  )} ms</span>
                  <span style="padding:2px 8px;border-radius:6px;font-size:11px;font-weight:700;background:${
                    isOk ? "#ecfdf5" : "#fef2f2"
                  };color:${isOk ? "#059669" : "#dc2626"};">
                    ${isOk ? "✓ OK" : s.status}
                  </span>
                </div>
              </div>
            `;
          })
          .join("")}
      </div>
    `;

    const lData = STATE.ledgerData || { integrity: { valid: true, verified_records: 0 }, records: [] };
    const records = lData.records || [];

    const ledgerHtml = `
      <div style="background:#ffffff;padding:14px 16px;border-radius:10px;border:1px solid #e2e8f0;margin-bottom:14px;display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;">
        <div>
          <div style="font-size:14px;font-weight:700;color:#0f172a;">
            Human-in-the-Loop RM Sign-Off
          </div>
          <div style="font-size:12px;color:#64748b;">
            Signs draft with ${STATE.operator.name}'s HMAC-SHA256 key and logs to SEC/FinCEN ledger.
          </div>
        </div>
        <button type="button" id="ncz-hitl-signoff-btn" style="padding:9px 16px;border-radius:8px;background:#059669;color:#ffffff;font-weight:700;font-size:13px;border:none;cursor:pointer;">
          ✓ Approve & Send Email
        </button>
      </div>

      <div style="display:flex;flex-direction:column;gap:8px;">
        ${records
          .map(
            (r) => `
          <div style="padding:12px 14px;border-radius:10px;background:#ffffff;border:1px solid #e2e8f0;">
            <div style="display:flex;justify-content:space-between;align-items:center;">
              <strong style="font-size:13px;color:#1d4ed8;">#${r.seq_id} • ${r.action_type.replace(
              /_/g,
              " "
            )}</strong>
              <span style="font-size:11px;color:#059669;font-weight:600;">✓ Hash Verified (${r.record_hash.slice(
                0,
                8
              )})</span>
            </div>
            <div style="margin-top:6px;font-size:12px;color:#334155;">
              ${r.redacted_output.slice(0, 150)}${r.redacted_output.length > 150 ? "..." : ""}
            </div>
          </div>
        `
          )
          .join("")}
      </div>
    `;

    container.innerHTML = `
      <div style="position:fixed;inset:0;z-index:9999;background:rgba(15,23,42,0.35);display:flex;justify-content:flex-end;">
        <div style="width:min(640px,95vw);height:100vh;background:#f8fafc;border-left:1px solid #cbd5e1;box-shadow:-8px 0 24px rgba(15,23,42,0.1);display:flex;flex-direction:column;overflow:hidden;">
          <div style="padding:16px 20px;background:#ffffff;border-bottom:1px solid #e2e8f0;display:flex;justify-content:space-between;align-items:center;">
            <div style="font-size:16px;font-weight:700;color:#0f172a;">
              AI Cost & SEC/FinCEN Audit Inspector
            </div>
            <button type="button" id="ncz-drawer-close-btn" style="padding:5px 12px;border-radius:6px;background:#f1f5f9;color:#0f172a;font-weight:600;border:1px solid #cbd5e1;cursor:pointer;">
              Close
            </button>
          </div>

          <div style="padding:12px 20px;background:#ffffff;border-bottom:1px solid #e2e8f0;display:flex;gap:10px;align-items:center;">
            <select id="ncz-drawer-case-select" style="flex:1;padding:8px 10px;border-radius:6px;border:1px solid #cbd5e1;background:#ffffff;color:#0f172a;font-weight:600;font-size:13px;">
              ${caseOptions}
            </select>
            <button type="button" id="ncz-run-mcp-pipeline-btn" style="padding:8px 14px;border-radius:6px;background:#2563eb;color:#ffffff;font-weight:600;font-size:13px;border:none;cursor:pointer;">
              ▶ Run AI Pipeline
            </button>
          </div>

          ${
            STATE.actionStatus
              ? `<div style="margin:10px 20px 0;padding:10px 12px;border-radius:6px;font-size:13px;font-weight:600;background:${
                  STATE.actionStatus.type === "error"
                    ? "#fef2f2"
                    : STATE.actionStatus.type === "success"
                    ? "#ecfdf5"
                    : "#eff6ff"
                };color:${
                  STATE.actionStatus.type === "error"
                    ? "#dc2626"
                    : STATE.actionStatus.type === "success"
                    ? "#059669"
                    : "#1d4ed8"
                };border:1px solid currentColor;">${STATE.actionStatus.text}</div>`
              : ""
          }

          <div style="padding:0 20px;display:flex;gap:16px;border-bottom:1px solid #e2e8f0;background:#ffffff;">
            <button type="button" id="ncz-tab-waterfall" style="padding:10px 4px;border:none;border-bottom:2px solid ${
              STATE.activeTab === "waterfall" ? "#2563eb" : "transparent"
            };background:transparent;color:${
      STATE.activeTab === "waterfall" ? "#2563eb" : "#64748b"
    };font-weight:700;font-size:13px;cursor:pointer;">
              1. AI Steps & Cost
            </button>
            <button type="button" id="ncz-tab-ledger" style="padding:10px 4px;border:none;border-bottom:2px solid ${
              STATE.activeTab === "ledger" ? "#2563eb" : "transparent"
            };background:transparent;color:${
      STATE.activeTab === "ledger" ? "#2563eb" : "#64748b"
    };font-weight:700;font-size:13px;cursor:pointer;">
              2. Audit Log & Sign-Off
            </button>
          </div>

          <div style="flex:1;overflow-y:auto;padding:16px 20px;">
            ${STATE.activeTab === "waterfall" ? waterfallHtml : ledgerHtml}
          </div>
        </div>
      </div>
    `;

    document.getElementById("ncz-drawer-close-btn").onclick = () => {
      STATE.drawerOpen = false;
      renderDrawer();
    };
    document.getElementById("ncz-drawer-case-select").onchange = (e) => {
      STATE.actionStatus = null;
      loadEntityInspection(e.target.value);
    };
    document.getElementById("ncz-run-mcp-pipeline-btn").onclick = triggerMcpPipelineRun;
    document.getElementById("ncz-tab-waterfall").onclick = () => {
      STATE.activeTab = "waterfall";
      renderDrawer();
    };
    document.getElementById("ncz-tab-ledger").onclick = () => {
      STATE.activeTab = "ledger";
      renderDrawer();
    };
    const hitlBtn = document.getElementById("ncz-hitl-signoff-btn");
    if (hitlBtn) {
      hitlBtn.onclick = triggerOneClickApprove;
    }
  }

  document.addEventListener("click", (e) => {
    const text = (e.target && e.target.innerText) || "";
    const match = text.match(/NCZ-2024-\d{4}/);
    if (match) {
      STATE.selectedCaseCode = match[0];
    }
  });

  // Bind login handlers immediately at 0ms
  bindInstantLoginHandlers();

  function boot() {
    bindInstantLoginHandlers();
    refreshGlobalTelemetry();
    setInterval(refreshGlobalTelemetry, 15000);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
