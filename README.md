# Northgate ChaseZero — Autonomous Enterprise KYC Platform

Multi-agent KYC document chasing platform powered by **LangGraph**, **FastAPI**, **Async SQLAlchemy 2.0 (SQLite)**, and **React 18 + Tailwind CSS v4**.

## Features

1. **Orchestrator Inbox (`/`)**:
   - Tracks 8 institutional corporate onboarding pipelines (`Apex Global Commodities Ltd`, `Meridian Capital Partners Fund IV`, `Aethelgard Global Holdings Ltd`, `Vanguard Logistics Nordics AS`, `Helios Shipping Syndicate Ltd`, `Aura Renewable Tech Holdings`, `Sovereign Ridge Capital LLC`, `Albion Trade Finance PLC`).
   - Live filtering by `All Active`, `SLA Breached (>3 Days)`, `Draft Ready`, `On Schedule / Completed`, search bar, and region filter.
2. **Case Tracker & Audit**:
   - Interactive KYC Document Matrix (`Missing` → `Received` → `Verified` status toggle).
   - Chase Cadence timeline, counterparty details, editable RM relationship notes persisted to SQLite, and 1-click LangGraph launch.
3. **ChaseZero AI Drafting Agent**:
   - Live SSE token streaming (`/api/cases/{case_id}/workflow/stream`) across the 3-node LangGraph `StateGraph` (`Orchestrator Node` → `Drafter Node` → `Critic QA Node`).
   - Optional `Test Critic Reject Loop` toggle demonstrating autonomous self-correction when the Critic intercepts a policy violation.
4. **Critic Compliance QA**:
   - 5-point regulatory guardrail audit (`Tone & Register`, `Document Accuracy`, `Promise & Liability Check`, `PII & Security Protocol`, `Regulatory Disclosures`) with interactive simulation and re-scan.
5. **Human-in-the-Loop (HITL) Dispatch Gateway**:
   - Rich email editor, Quick Tone Adjusters (`Slightly More Casual`, `More Urgent / Deadline Focus`, `Add Direct Calendar Link`), CC management, cryptographic audit hash, and RM approval & dispatch.
6. **Executive Success & 2-Week Pilot Review (COO Dashboard)**:
   - Real-time operational velocity, case-days saved, RM adoption rate, timeline compression, participating desks, and live `agent_actions` SQLite telemetry ledger.
7. **OpenAPI / Swagger UI (`/docs` & `/openapi.json`)**:
   - Complete REST + SSE API documentation matching the reference deployment.

## Quick Start

```powershell
pip install -r requirements.txt
uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload
```

- **Web App**: http://localhost:8000/
- **API Docs (Swagger UI)**: http://localhost:8000/docs
- **OpenAPI Spec**: http://localhost:8000/openapi.json
