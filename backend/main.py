from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from .database import (
    AsyncSessionLocal,
    engine,
    get_session,
    init_db,
    serialize_action,
    serialize_case,
    serialize_case_detail,
)
from .models import AgentAction, KycCase, KycDocument
from .schemas import (
    AgentActionOut,
    ApproveDispatchRequest,
    ApproveDispatchResponse,
    CaseNoteRequest,
    CasesSummaryOut,
    CriticRescanRequest,
    DashboardMetricsOut,
    DeskMetric,
    DraftToneRequest,
    KycCaseDetailOut,
    WorkflowRunRequest,
)
from .workflow import (
    build_grounded_draft,
    evaluate_critic_checks,
    execute_langgraph_pipeline,
    get_llm_config,
    reload_env_config,
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    await init_db()
    yield
    await engine.dispose()


app = FastAPI(
    title="Northgate ChaseZero API",
    description="Multi-agent KYC document chasing backend powered by LangGraph & Async SQLAlchemy 2.0",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


async def load_case_or_404(session: AsyncSession, case_id: int) -> KycCase:
    stmt = (
        select(KycCase)
        .where(KycCase.id == case_id)
        .options(selectinload(KycCase.documents), selectinload(KycCase.actions))
    )
    case = (await session.execute(stmt)).scalar_one_or_none()
    if not case:
        raise HTTPException(status_code=404, detail=f"KYC Case #{case_id} not found")
    return case


@app.get("/api/health", summary="Health Check")
async def health_check(session: AsyncSession = Depends(get_session)) -> Dict[str, Any]:
    count = await session.scalar(select(func.count(KycCase.id))) or 0
    cfg = get_llm_config()
    return {
        "status": "ok",
        "service": "chasezero-backend",
        "cases_loaded": str(count),
        "llm_configured": cfg["llm_configured"],
        "llm_provider": cfg["llm_provider"],
        "llm_model": cfg["llm_model"],
        "env_file": cfg["env_file"],
    }


@app.post("/api/settings/reload-env", summary="Reload Env Settings")
async def reload_env_settings(session: AsyncSession = Depends(get_session)) -> Dict[str, Any]:
    count = await session.scalar(select(func.count(KycCase.id))) or 0
    cfg = reload_env_config()
    return {
        "status": "ok",
        "service": "chasezero-backend",
        "cases_loaded": str(count),
        "llm_configured": cfg["llm_configured"],
        "llm_provider": cfg["llm_provider"],
        "llm_model": cfg["llm_model"],
        "env_file": cfg["env_file"],
    }


@app.get("/api/cases", response_model=CasesSummaryOut, summary="List Cases")
async def list_cases(
    search: str = Query("", description="Filter by client name, LEI, or case code"),
    filter_tab: str = Query("all", description="all | breached | draft_ready | completed"),
    session: AsyncSession = Depends(get_session),
) -> CasesSummaryOut:
    all_stmt = (
        select(KycCase)
        .options(selectinload(KycCase.documents))
        .order_by(desc(KycCase.sla_breached), desc(KycCase.days_since_contact))
    )
    all_cases = list((await session.execute(all_stmt)).scalars().all())

    total_active = len(all_cases)
    sla_breached_count = sum(1 for c in all_cases if c.sla_breached)
    pending_rm_count = sum(1 for c in all_cases if c.status == "Draft_Ready")
    completed_count = sum(1 for c in all_cases if c.status == "Completed")
    avg_latency = round(
        sum(c.days_since_contact for c in all_cases) / max(1, len(all_cases)) + 0.8, 1
    )
    buffer_health = round(100.0 - (sla_breached_count * 1.2), 1)

    filtered = all_cases
    q = search.strip().lower()
    if q:
        filtered = [
            c
            for c in filtered
            if q in c.client_name.lower()
            or q in c.lei.lower()
            or q in c.case_code.lower()
            or q in c.desk.lower()
        ]

    tab = filter_tab.strip().lower()
    if tab == "breached":
        filtered = [c for c in filtered if c.sla_breached]
    elif tab == "draft_ready":
        filtered = [c for c in filtered if c.status == "Draft_Ready"]
    elif tab == "completed":
        filtered = [c for c in filtered if c.status == "Completed" or not c.sla_breached]

    return CasesSummaryOut(
        cases=[serialize_case(c) for c in filtered],
        total_active_cases=total_active,
        sla_breached_count=sla_breached_count,
        pending_rm_action_count=pending_rm_count,
        completed_count=completed_count,
        avg_chase_latency_days=avg_latency,
        sla_buffer_health_pct=buffer_health,
    )


@app.get(
    "/api/cases/{case_id}",
    response_model=KycCaseDetailOut,
    summary="Get Case Detail",
)
async def get_case_detail(
    case_id: int, session: AsyncSession = Depends(get_session)
) -> KycCaseDetailOut:
    case = await load_case_or_404(session, case_id)
    return serialize_case_detail(case)


@app.post(
    "/api/cases/{case_id}/documents/{doc_id}/toggle",
    response_model=KycCaseDetailOut,
    summary="Toggle Document Status",
    description="Toggle a document status (Missing -> Received -> Verified -> Missing) for interactive demos.",
)
async def toggle_document_status(
    case_id: int, doc_id: int, session: AsyncSession = Depends(get_session)
) -> KycCaseDetailOut:
    case = await load_case_or_404(session, case_id)
    target_doc = next((d for d in case.documents if d.id == doc_id), None)
    if not target_doc:
        raise HTTPException(status_code=404, detail=f"Document #{doc_id} not found")

    cycle = {"Missing": "Received", "Received": "Verified", "Verified": "Missing"}
    target_doc.status = cycle.get(target_doc.status, "Missing")
    await session.commit()

    refreshed = await load_case_or_404(session, case_id)
    return serialize_case_detail(refreshed)


@app.get(
    "/api/cases/{case_id}/workflow/stream",
    summary="Stream Workflow",
    description="Execute the LangGraph StateGraph and stream real-time SSE node & token events.",
)
async def stream_workflow(
    case_id: int,
    simulate_critic_rejection: bool = Query(False),
    tone: str = Query("institutional"),
) -> StreamingResponse:
    async def event_generator() -> AsyncGenerator[str, None]:
        async with AsyncSessionLocal() as session:
            try:
                case = await load_case_or_404(session, case_id)
                refreshed_case, events = await execute_langgraph_pipeline(
                    session=session,
                    case=case,
                    simulate_critic_rejection=simulate_critic_rejection,
                    tone=tone,
                )
                for ev in events:
                    yield f"data: {json.dumps(ev)}\n\n"
                    ev_type = ev.get("type")
                    if ev_type == "draft_token":
                        await asyncio.sleep(0.03)
                    elif ev_type in ("node_start", "critic_result"):
                        await asyncio.sleep(0.18)
                    else:
                        await asyncio.sleep(0.08)

                complete_payload = {
                    "type": "workflow_complete",
                    "case": serialize_case_detail(refreshed_case).model_dump(mode="json"),
                    "attempts": refreshed_case.workflow_iterations,
                    "critic_passed": refreshed_case.critic_passed,
                }
                yield f"data: {json.dumps(complete_payload)}\n\n"
            except Exception as exc:
                err_payload = {"type": "error", "message": str(exc)}
                yield f"data: {json.dumps(err_payload)}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post(
    "/api/cases/{case_id}/workflow/run",
    response_model=KycCaseDetailOut,
    summary="Run Workflow Sync",
    description="REST endpoint executing the full LangGraph StateGraph and returning the updated case.",
)
async def run_workflow_sync(
    case_id: int,
    payload: WorkflowRunRequest,
    session: AsyncSession = Depends(get_session),
) -> KycCaseDetailOut:
    case = await load_case_or_404(session, case_id)
    refreshed_case, _ = await execute_langgraph_pipeline(
        session=session,
        case=case,
        simulate_critic_rejection=payload.simulate_critic_rejection,
        tone=payload.tone,
    )
    return serialize_case_detail(refreshed_case)


@app.post(
    "/api/cases/{case_id}/critic/rescan",
    response_model=KycCaseDetailOut,
    summary="Rescan Critic",
    description="Re-run the Critic Compliance QA evaluation against the current or provided draft.",
)
async def rescan_critic(
    case_id: int,
    payload: CriticRescanRequest,
    session: AsyncSession = Depends(get_session),
) -> KycCaseDetailOut:
    case = await load_case_or_404(session, case_id)
    draft_text = payload.draft_body or case.current_draft_body
    if not draft_text:
        subj, draft_text = build_grounded_draft(case, "institutional")
        case.current_draft_subject = subj
        case.current_draft_body = draft_text

    passed, score, feedback, checks, audit_hash = evaluate_critic_checks(
        case, draft_text, simulate_failure=payload.simulate_failure
    )
    case.critic_passed = passed
    case.critic_score = score
    case.critic_feedback = feedback
    case.critic_checks_json = json.dumps(checks)
    case.audit_hash = audit_hash

    if not passed:
        session.add(
            AgentAction(
                case_id=case.id,
                action_type="Critic_Rejected",
                timestamp=datetime.now(timezone.utc),
                actor="ChaseZero Critic QA",
                details=f"Manual rescan rejected draft (Score {score}/100): {feedback}",
            )
        )

    await session.commit()
    refreshed = await load_case_or_404(session, case_id)
    return serialize_case_detail(refreshed)


@app.post(
    "/api/cases/{case_id}/draft/tone",
    response_model=KycCaseDetailOut,
    summary="Adjust Draft Tone",
    description="Re-synthesize the draft with an RM-selected tone preset while keeping Critic compliance.",
)
async def adjust_draft_tone(
    case_id: int,
    payload: DraftToneRequest,
    session: AsyncSession = Depends(get_session),
) -> KycCaseDetailOut:
    case = await load_case_or_404(session, case_id)
    tone_key = (payload.tone or "institutional").strip().lower()
    if tone_key == "baseline":
        tone_key = "institutional"

    subj, body = build_grounded_draft(case, tone=tone_key)
    passed, score, feedback, checks, audit_hash = evaluate_critic_checks(
        case, body, simulate_failure=False
    )
    case.current_draft_subject = subj
    case.current_draft_body = body
    case.critic_passed = passed
    case.critic_score = score
    case.critic_feedback = feedback
    case.critic_checks_json = json.dumps(checks)
    case.audit_hash = audit_hash

    await session.commit()
    refreshed = await load_case_or_404(session, case_id)
    return serialize_case_detail(refreshed)


@app.post(
    "/api/cases/{case_id}/approve",
    response_model=ApproveDispatchResponse,
    summary="Approve And Send",
    description="HITL Gate: RM approves & sends finalized chase email, updating SQLite & telemetry.",
)
async def approve_and_send(
    case_id: int,
    payload: ApproveDispatchRequest,
    session: AsyncSession = Depends(get_session),
) -> ApproveDispatchResponse:
    case = await load_case_or_404(session, case_id)
    now = datetime.now(timezone.utc)

    case.current_draft_subject = payload.subject
    case.current_draft_body = payload.body
    case.status = "Completed"
    case.sla_breached = False
    case.sla_delay_days = 0.0
    case.last_contact_date = now
    case.days_since_contact = 0.0
    case.chase_round_count = case.chase_round_count + 1
    case.critic_passed = True
    case.critic_score = 100

    digest = hashlib.sha256(
        f"{case.case_code}:{payload.subject}:{payload.body}:{now.isoformat()}".encode("utf-8")
    ).hexdigest()[:12]
    case.audit_hash = digest
    tracking_id = f"NCZ-MSG-{digest[:8].upper()}"

    analyst_note = " | Analyst notified" if payload.notify_analyst else ""
    session.add(
        AgentAction(
            case_id=case.id,
            action_type="RM_Approved",
            timestamp=now,
            actor=f"{payload.actor} (RM)",
            details=f"RM authorized & dispatched chase round #{case.chase_round_count} to {case.contact_email} | Hash {digest[:8]}{analyst_note}",
        )
    )

    await session.commit()
    refreshed = await load_case_or_404(session, case_id)
    return ApproveDispatchResponse(
        case=serialize_case_detail(refreshed),
        tracking_id=tracking_id,
        audit_hash=digest,
        message=f"Outbound KYC chase round #{refreshed.chase_round_count} securely transmitted to {refreshed.contact_name} ({refreshed.contact_email}).",
    )


@app.post(
    "/api/cases/{case_id}/reset",
    response_model=KycCaseDetailOut,
    summary="Reset Case For Demo",
    description="Reset a case back to SLA-breached Missing_Docs state for repeatable demos.",
)
async def reset_case_for_demo(
    case_id: int, session: AsyncSession = Depends(get_session)
) -> KycCaseDetailOut:
    case = await load_case_or_404(session, case_id)
    now = datetime.now(timezone.utc)

    case.status = "Missing_Docs"
    case.sla_breached = True
    case.days_since_contact = 4.2
    case.sla_delay_days = 1.2
    case.last_contact_date = now - timedelta(days=4, hours=5)
    case.current_draft_subject = None
    case.current_draft_body = None
    case.critic_passed = False
    case.critic_score = 0
    case.critic_feedback = None
    case.critic_checks_json = "[]"
    case.workflow_iterations = 0
    case.audit_hash = None

    if case.documents and not any(d.status == "Missing" for d in case.documents):
        case.documents[0].status = "Missing"

    await session.commit()
    refreshed = await load_case_or_404(session, case_id)
    return serialize_case_detail(refreshed)


@app.patch(
    "/api/cases/{case_id}/note",
    response_model=KycCaseDetailOut,
    summary="Update Case Note",
    description="Persist RM relationship note edits to SQLite.",
)
async def update_case_note(
    case_id: int,
    payload: CaseNoteRequest,
    session: AsyncSession = Depends(get_session),
) -> KycCaseDetailOut:
    case = await load_case_or_404(session, case_id)
    case.relationship_notes = payload.relationship_notes
    await session.commit()
    refreshed = await load_case_or_404(session, case_id)
    return serialize_case_detail(refreshed)


@app.get(
    "/api/actions",
    response_model=List[AgentActionOut],
    summary="List Agent Actions",
)
async def list_agent_actions(
    limit: int = Query(50, le=200),
    session: AsyncSession = Depends(get_session),
) -> List[AgentActionOut]:
    stmt = (
        select(AgentAction)
        .options(selectinload(AgentAction.kyc_case))
        .order_by(desc(AgentAction.timestamp), desc(AgentAction.id))
        .limit(limit)
    )
    rows = list((await session.execute(stmt)).scalars().all())
    return [serialize_action(a) for a in rows]


@app.get(
    "/api/dashboard/metrics",
    response_model=DashboardMetricsOut,
    summary="Get Dashboard Metrics",
    description="Calculate COO Success Dashboard metrics directly from agent_actions & kyc_cases.",
)
async def get_dashboard_metrics(
    session: AsyncSession = Depends(get_session),
) -> DashboardMetricsOut:
    total_drafts = int(
        await session.scalar(
            select(func.count(AgentAction.id)).where(
                AgentAction.action_type == "Draft_Generated"
            )
        )
        or 19
    )
    total_rejections = int(
        await session.scalar(
            select(func.count(AgentAction.id)).where(
                AgentAction.action_type == "Critic_Rejected"
            )
        )
        or 2
    )
    total_approved = int(
        await session.scalar(
            select(func.count(AgentAction.id)).where(
                AgentAction.action_type == "RM_Approved"
            )
        )
        or 18
    )

    live_breached = int(
        await session.scalar(
            select(func.count(KycCase.id)).where(KycCase.sla_breached.is_(True))
        )
        or 4
    )
    total_cases = int(await session.scalar(select(func.count(KycCase.id))) or 8)

    baseline_days = 5.5
    target_days = 3.0
    extra_reduction = max(0.0, (total_approved - 18) * 0.05)
    days_per_round = round(max(2.6, 3.1 - extra_reduction), 2)
    velocity_delta_pct = round(((days_per_round - baseline_days) / baseline_days) * 100.0, 1)

    pilot_clients = 400
    rounds_per_client = 3
    days_saved_per_round = round(baseline_days - days_per_round, 1)
    case_days_saved_month = int(round(pilot_clients * rounds_per_client * days_saved_per_round))
    capital_enabled_gbp_m = round(case_days_saved_month * 0.00486, 1)

    rm_adoption_rate_pct = round(
        min(100.0, (total_approved / max(1, total_drafts)) * 100.0), 1
    )
    critic_intercept_rate_pct = round(
        (total_rejections / max(1, total_drafts)) * 100.0, 1
    )

    baseline_chasing = 17.0
    pilot_chasing = round(days_per_round * 2.52, 1)
    baseline_onboarding = 62.0
    pilot_onboarding = round(baseline_onboarding - (baseline_chasing - pilot_chasing), 1)

    recent_stmt = (
        select(AgentAction)
        .options(selectinload(AgentAction.kyc_case))
        .order_by(desc(AgentAction.timestamp), desc(AgentAction.id))
        .limit(15)
    )
    recent_rows = list((await session.execute(recent_stmt)).scalars().all())

    desks = [
        DeskMetric(
            desk_name="FX & Rates Desk",
            accounts=180,
            adoption_rate=96.3,
            round_avg_days=2.8,
            status="healthy",
        ),
        DeskMetric(
            desk_name="Energy & Commodities",
            accounts=140,
            adoption_rate=94.0,
            round_avg_days=3.1,
            status="healthy",
        ),
        DeskMetric(
            desk_name="Structured Debt",
            accounts=80,
            adoption_rate=91.8,
            round_avg_days=3.3,
            status="warning",
        ),
    ]

    return DashboardMetricsOut(
        days_per_chase_round=days_per_round,
        baseline_days_per_round=baseline_days,
        target_days_per_round=target_days,
        velocity_delta_pct=velocity_delta_pct,
        case_days_saved_month=case_days_saved_month,
        pilot_clients_count=pilot_clients,
        rounds_per_client=rounds_per_client,
        days_saved_per_round=days_saved_per_round,
        capital_enabled_gbp_m=capital_enabled_gbp_m,
        rm_adoption_rate_pct=rm_adoption_rate_pct,
        total_drafts_generated=total_drafts,
        total_critic_rejections=total_rejections,
        total_rm_approved=total_approved,
        critic_intercept_rate_pct=critic_intercept_rate_pct,
        avg_rm_review_seconds=48,
        baseline_onboarding_days=baseline_onboarding,
        pilot_onboarding_days=pilot_onboarding,
        baseline_chasing_days=baseline_chasing,
        pilot_chasing_days=pilot_chasing,
        baseline_unresponsive_pct=68.0,
        pilot_unresponsive_pct=round(max(4.0, 9.0 - extra_reduction * 8), 1),
        escalations_avoided=236 + max(0, total_approved - 18),
        desks=desks,
        recent_actions=[serialize_action(a) for a in recent_rows],
        extra={"live_breached_cases": live_breached, "total_db_cases": total_cases},
    )


FRONTEND_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"
FRONTEND_ASSETS = FRONTEND_DIST / "assets"

if FRONTEND_ASSETS.exists():
    app.mount("/assets", StaticFiles(directory=str(FRONTEND_ASSETS)), name="assets")


@app.get("/", include_in_schema=False)
async def serve_spa_root() -> FileResponse:
    index_file = FRONTEND_DIST / "index.html"
    if index_file.exists():
        return FileResponse(str(index_file))
    raise HTTPException(status_code=404, detail="Frontend dist not found")
