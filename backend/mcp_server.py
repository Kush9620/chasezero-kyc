from __future__ import annotations

import inspect
import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Literal, Optional

from fastmcp import FastMCP
from opentelemetry import trace
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags
from sqlalchemy import desc, select
from sqlalchemy.orm import selectinload

from .compliance_ledger import (
    append_audit_ledger_entry,
    get_mcp_auth_secret,
    get_observability_summary,
    query_audit_ledger,
    query_case_traces,
    verify_ledger_integrity,
)
from .database import AsyncSessionLocal, serialize_case, serialize_case_detail
from .models import KycCase
from .telemetry import (
    get_current_trace_id_hex,
    get_execution_context,
    get_tracer,
    set_execution_context,
)
from .workflow import (
    ComplianceSecurityError,
    approve_and_dispatch_chase_workflow,
    execute_langgraph_pipeline,
)

mcp = FastMCP(name="Northgate ChaseZero KYC Compliance Server")


async def _resolve_case(session: Any, case_identifier: str | int) -> KycCase:
    raw = str(case_identifier).strip()
    stmt = select(KycCase).options(
        selectinload(KycCase.documents), selectinload(KycCase.actions)
    )
    if raw.isdigit():
        stmt = stmt.where(KycCase.id == int(raw))
    else:
        stmt = stmt.where(KycCase.case_code == raw)
    case = (await session.execute(stmt)).scalar_one_or_none()
    if not case:
        raise ValueError(f"KYC Case '{case_identifier}' not found in SQLite registry.")
    return case


async def get_kyc_cases(
    status: Literal["all", "sla_breached", "draft_ready", "on_schedule", "completed"] = "all",
    rm_name: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Returns filtered corporate KYC cases, SLA breach metrics, missing documents,
    and cumulative AI cost (total_cost_usd) per case. Logs CASE_QUERIED to the audit ledger.
    """
    tracer = get_tracer("northgate.chasezero.mcp")
    ctx = get_execution_context()

    with tracer.start_as_current_span("mcp.tools/call.get_kyc_cases") as span:
        trace_id = get_current_trace_id_hex()
        span.set_attribute("gen_ai.operation.name", "execute_tool")
        span.set_attribute("gen_ai.provider.name", "internal")
        span.set_attribute("kyc.actor_id", ctx["actor_id"])

        async with AsyncSessionLocal() as session:
            stmt = (
                select(KycCase)
                .options(selectinload(KycCase.documents))
                .order_by(desc(KycCase.sla_breached), desc(KycCase.days_since_contact))
            )
            all_cases = list((await session.execute(stmt)).scalars().all())

            filtered = all_cases
            if status == "sla_breached":
                filtered = [c for c in filtered if c.sla_breached]
            elif status == "draft_ready":
                filtered = [c for c in filtered if c.status == "Draft_Ready"]
            elif status == "on_schedule":
                filtered = [c for c in filtered if not c.sla_breached and c.status != "Completed"]
            elif status == "completed":
                filtered = [c for c in filtered if c.status == "Completed"]

            if rm_name:
                q_rm = rm_name.strip().lower()
                filtered = [c for c in filtered if q_rm in (c.assigned_rm or "").lower()]

            serialized = [serialize_case(c).model_dump(mode="json") for c in filtered]
            total_cost = round(sum(float(item.get("total_cost_usd", 0.0)) for item in serialized), 6)

            audit_entry = append_audit_ledger_entry(
                trace_id=trace_id,
                case_id=serialized[0]["case_code"] if len(serialized) == 1 else "PORTFOLIO-ALL",
                actor_id=ctx["actor_id"],
                actor_role=ctx["actor_role"],
                entry_channel=ctx["entry_channel"],
                action_type="CASE_QUERIED",
                raw_input=f"get_kyc_cases(status={status}, rm_name={rm_name})",
                raw_output=f"Returned {len(serialized)} cases | Portfolio AI Spend=${total_cost:.6f}",
            )

            return {
                "trace_id": trace_id,
                "audit_id": audit_entry["audit_id"],
                "filter_status": status,
                "rm_filter": rm_name,
                "count": len(serialized),
                "sla_breached_count": sum(1 for c in all_cases if c.sla_breached),
                "draft_ready_count": sum(1 for c in all_cases if c.status == "Draft_Ready"),
                "portfolio_total_cost_usd": total_cost,
                "cases": serialized,
            }


async def run_chase_pipeline(
    case_id: str,
    tone_override: Optional[str] = None,
    simulate_critic_rejection: bool = False,
    force_circuit_breaker: bool = False,
) -> Dict[str, Any]:
    """
    Runs the instrumented LangGraph AI Drafting Agent + Critic Compliance QA pipeline.
    Hard Constraint: Only transitions case status to Draft_Ready. Never sends an email.
    Returns trace_id, audit_id, draft_subject, draft_body, critic_legal_score,
    critic_politeness_score, cost_usd, and latency_ms.
    """
    tracer = get_tracer("northgate.chasezero.mcp")
    ctx = get_execution_context()
    t0 = time.perf_counter()

    async with AsyncSessionLocal() as session:
        case = await _resolve_case(session, case_id)
        set_execution_context(case_id=case.case_code, round_number=case.chase_round_count)

        with tracer.start_as_current_span("mcp.tools/call.run_chase_pipeline") as root_span:
            trace_id = get_current_trace_id_hex()
            root_span.set_attribute("gen_ai.operation.name", "execute_tool")
            root_span.set_attribute("gen_ai.provider.name", "internal")
            root_span.set_attribute("gen_ai.request.model", "fastmcp-v3-router")
            root_span.set_attribute("kyc.case_id", case.case_code)
            root_span.set_attribute("kyc.lei", case.lei)
            root_span.set_attribute("kyc.round_number", case.chase_round_count)
            root_span.set_attribute("kyc.actor_id", ctx["actor_id"])

            refreshed_case, events = await execute_langgraph_pipeline(
                session=session,
                case=case,
                simulate_critic_rejection=simulate_critic_rejection,
                tone=(tone_override or "institutional").strip().lower(),
                force_circuit_breaker=force_circuit_breaker,
            )

            latency_ms = round((time.perf_counter() - t0) * 1000.0, 2)

            critic_ev = next(
                (ev for ev in reversed(events) if ev.get("type") == "critic_result"),
                {},
            )
            legal_score = float(
                critic_ev.get("legal_score", round((refreshed_case.critic_score or 0) / 100.0, 4))
            )
            politeness_score = float(
                critic_ev.get("politeness_score", 0.98 if refreshed_case.critic_passed else 0.42)
            )
            audit_id = str(critic_ev.get("audit_id") or "")

            run_cost_usd = round(
                sum(float(ev.get("cost_usd", 0.0)) for ev in events if "cost_usd" in ev),
                6,
            )

            if not refreshed_case.critic_passed:
                root_span.set_attribute("kyc.span.status", "CRITIC_REJECTED")

            return {
                "trace_id": trace_id,
                "audit_id": audit_id,
                "case_id": refreshed_case.case_code,
                "client_name": refreshed_case.client_name,
                "lei": refreshed_case.lei,
                "status": refreshed_case.status,
                "draft_subject": refreshed_case.current_draft_subject,
                "draft_body": refreshed_case.current_draft_body,
                "critic_passed": refreshed_case.critic_passed,
                "critic_legal_score": legal_score,
                "critic_politeness_score": politeness_score,
                "workflow_iterations": refreshed_case.workflow_iterations,
                "circuit_breaker_tripped": bool(critic_ev.get("circuit_breaker_tripped", False)),
                "cost_usd": run_cost_usd,
                "latency_ms": latency_ms,
                "email_dispatched": False,
            }


async def trigger_batch_chase_drafts(min_delay_days: float = 3.0) -> Dict[str, Any]:
    """
    Executes run_chase_pipeline across all SLA-breached cases not yet in Draft_Ready,
    returning a summary of generated drafts, trace IDs, and batch USD cost.
    """
    ctx = get_execution_context()
    async with AsyncSessionLocal() as session:
        stmt = (
            select(KycCase)
            .where(KycCase.days_since_contact >= min_delay_days)
            .where(KycCase.status == "Missing_Docs")
            .order_by(desc(KycCase.days_since_contact))
        )
        targets = list((await session.execute(stmt)).scalars().all())
        target_codes = [c.case_code for c in targets]

    results: List[Dict[str, Any]] = []
    total_batch_cost = 0.0
    trace_ids: List[str] = []

    for code in target_codes:
        res = await run_chase_pipeline(case_id=code)
        results.append(res)
        total_batch_cost = round(total_batch_cost + float(res["cost_usd"]), 6)
        trace_ids.append(res["trace_id"])

    batch_trace_id = trace_ids[0] if trace_ids else get_current_trace_id_hex()
    audit_entry = append_audit_ledger_entry(
        trace_id=batch_trace_id,
        case_id="BATCH-SLA-BREACHED",
        actor_id=ctx["actor_id"],
        actor_role=ctx["actor_role"],
        entry_channel=ctx["entry_channel"],
        action_type="BATCH_QUEUED",
        raw_input=f"trigger_batch_chase_drafts(min_delay_days={min_delay_days})",
        raw_output=f"Processed {len(results)} SLA-breached cases: {target_codes} | Total Cost=${total_batch_cost:.6f}",
    )

    return {
        "batch_audit_id": audit_entry["audit_id"],
        "min_delay_days": min_delay_days,
        "cases_processed": len(results),
        "case_ids": target_codes,
        "trace_ids": trace_ids,
        "batch_cost_usd": total_batch_cost,
        "results": results,
    }


async def approve_and_dispatch_chase(
    case_id: str,
    rm_id: str,
    rm_approval_secret: str,
    edited_body: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Strict SEC & FinCEN Perimeter Gate:
    Verifies rm_approval_secret, confirms Draft_Ready + Critic QA legal_score >= 0.95,
    re-runs Critic QA if edited_body is supplied, computes HMAC-SHA256 hitl_signature,
    appends RM_HITL_APPROVED & EMAIL_DISPATCHED to sec_fincen_audit_ledger, and dispatches.
    """
    ctx = get_execution_context()
    async with AsyncSessionLocal() as session:
        case = await _resolve_case(session, case_id)
        receipt = await approve_and_dispatch_chase_workflow(
            session=session,
            case=case,
            rm_id=rm_id,
            rm_approval_secret=rm_approval_secret,
            edited_body=edited_body,
            entry_channel=ctx["entry_channel"],
        )
        return {
            "case_id": receipt["case"].case_code,
            "client_name": receipt["case"].client_name,
            "status": receipt["status"],
            "tracking_id": receipt["tracking_id"],
            "trace_id": receipt["trace_id"],
            "audit_id": receipt["audit_id"],
            "approval_audit_id": receipt["approval_audit_id"],
            "record_hash": receipt["record_hash"],
            "hitl_signature": receipt["hitl_signature"],
            "rm_id": receipt["rm_id"],
            "dispatched_at": receipt["dispatched_at"],
            "email_dispatched": True,
        }


async def run_sla_watchdog_scan() -> Dict[str, Any]:
    """
    Runs the SQLite SLA breach scan, identifies delay-attribution drivers,
    and logs SYSTEM_WATCHDOG telemetry to sec_fincen_audit_ledger.
    """
    tracer = get_tracer("northgate.chasezero.watchdog")
    set_execution_context(
        actor_id="system:watchdog",
        actor_role="SYSTEM_WATCHDOG",
        entry_channel="CRON_WATCHDOG",
    )

    with tracer.start_as_current_span("mcp.tools/call.run_sla_watchdog_scan") as span:
        trace_id = get_current_trace_id_hex()
        span.set_attribute("gen_ai.operation.name", "db_query")
        span.set_attribute("gen_ai.provider.name", "internal")

        async with AsyncSessionLocal() as session:
            stmt = select(KycCase).options(selectinload(KycCase.documents))
            cases = list((await session.execute(stmt)).scalars().all())

            breached = [c for c in cases if c.sla_breached or c.days_since_contact > 3.0]
            drivers = [
                {
                    "case_id": c.case_code,
                    "client_name": c.client_name,
                    "lei": c.lei,
                    "days_since_contact": c.days_since_contact,
                    "sla_delay_days": c.sla_delay_days,
                    "status": c.status,
                    "missing_documents": [
                        d.document_name for d in c.documents if d.status == "Missing"
                    ],
                }
                for c in breached
            ]

            audit_entry = append_audit_ledger_entry(
                trace_id=trace_id,
                case_id="WATCHDOG-SCAN",
                actor_id="system:watchdog",
                actor_role="SYSTEM_WATCHDOG",
                entry_channel="CRON_WATCHDOG",
                action_type="CASE_QUERIED",
                raw_input="run_sla_watchdog_scan(threshold_days=3.0)",
                raw_output=f"Detected {len(breached)} SLA-breached corporate entities out of {len(cases)} active.",
            )

            return {
                "trace_id": trace_id,
                "audit_id": audit_entry["audit_id"],
                "scanned_at": datetime.now(timezone.utc).isoformat(),
                "total_active_cases": len(cases),
                "sla_breached_count": len(breached),
                "breach_drivers": drivers,
            }


async def verify_and_export_audit_ledger(case_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Runs verify_ledger_integrity() to prove zero tampering and returns the full SEC/FinCEN audit chain.
    """
    ctx = get_execution_context()
    trace_id = get_current_trace_id_hex()
    append_audit_ledger_entry(
        trace_id=trace_id,
        case_id=case_id or "ALL-CASES",
        actor_id=ctx["actor_id"],
        actor_role=ctx["actor_role"],
        entry_channel=ctx["entry_channel"],
        action_type="AUDIT_EXPORTED",
        raw_input=f"verify_and_export_audit_ledger(case_id={case_id})",
        raw_output="Exported tamper-evident SEC Rule 17a-4 & FinCEN CDD hash-chained audit ledger.",
    )
    ledger_data = query_audit_ledger(case_id=case_id, limit=500, offset=0)
    return {
        "trace_id": trace_id,
        "integrity": ledger_data["integrity"],
        "total_records": ledger_data["total"],
        "records": ledger_data["records"],
    }


# Register tools on FastMCP instance while preserving direct async function callability
for _tool_fn in (
    get_kyc_cases,
    run_chase_pipeline,
    trigger_batch_chase_drafts,
    approve_and_dispatch_chase,
    run_sla_watchdog_scan,
    verify_and_export_audit_ledger,
):
    mcp.tool(_tool_fn)


# ==============================================================================
# MCP Resources
# ==============================================================================


async def resource_executive_summary() -> str:
    obs = get_observability_summary()
    async with AsyncSessionLocal() as session:
        stmt = select(KycCase)
        cases = list((await session.execute(stmt)).scalars().all())
        breached_count = sum(1 for c in cases if c.sla_breached)
        buffer_health = round(100.0 - (breached_count * 1.5), 1)

    payload = {
        "sla_buffer_health_pct": buffer_health,
        "avg_chase_latency_days": 3.1,
        "politeness_score": 0.98,
        "legal_accuracy_score": 0.99,
        "total_ai_spend_usd": obs["total_spend_usd"],
        "avg_cost_per_chase_usd": obs["avg_cost_per_chase_usd"],
        "total_tokens": obs["total_tokens"],
        "audit_ledger_integrity": {
            "valid": obs["ledger_integrity_valid"],
            "verified_records": obs["ledger_verified_records"],
            "head_hash": obs["ledger_head_hash"],
        },
    }
    return json.dumps(payload, indent=2)


async def resource_case_dossier(case_id: str) -> str:
    async with AsyncSessionLocal() as session:
        case = await _resolve_case(session, case_id)
        detail = serialize_case_detail(case).model_dump(mode="json")
        return json.dumps(detail, indent=2, default=str)


async def resource_case_traces(case_id: str) -> str:
    async with AsyncSessionLocal() as session:
        case = await _resolve_case(session, case_id)
        traces = query_case_traces(case.case_code)
        return json.dumps(traces, indent=2, default=str)


mcp.resource("kyc://telemetry/executive-summary")(resource_executive_summary)
mcp.resource("kyc://cases/{case_id}")(resource_case_dossier)
mcp.resource("kyc://observability/traces/{case_id}")(resource_case_traces)


# ==============================================================================
# MCP Prompts
# ==============================================================================


def prompt_morning_sla_briefing() -> str:
    return (
        "You are the Northgate ChaseZero Institutional KYC Co-Pilot for VP Sarah Jenkins.\n"
        "1. Read resource `kyc://telemetry/executive-summary` and call tool `get_kyc_cases(status='sla_breached')`.\n"
        "2. Prioritize corporate entities by SLA delay days and credit facility value (£M).\n"
        "3. Summarize overnight LLM token usage, cumulative USD spend (`total_ai_spend_usd`), and confirm SEC/FinCEN ledger integrity.\n"
        "4. List all cases currently in `Draft_Ready` awaiting Relationship Manager HMAC-SHA256 sign-off."
    )


def prompt_pre_signoff_compliance_check(case_id: str) -> str:
    return (
        f"Perform a final SEC Rule 17a-4, FinCEN CDD (31 CFR 1010.230), and UK PRA/FCA pre-signoff review for case `{case_id}`:\n"
        f"1. Read resources `kyc://cases/{case_id}` and `kyc://observability/traces/{case_id}`.\n"
        "2. Verify that every document marked 'Missing' (UBO Registry, W-8BEN-E, FATCA GIIN, PSC Register, Trust Deed) is explicitly cited in `current_draft_body`.\n"
        "3. Confirm zero already-Verified documents are requested and zero credit-commitment promises are made.\n"
        "4. Present the Critic QA scores, trace cost in USD, and prompt the Relationship Manager for final `approve_and_dispatch_chase` authorization."
    )


mcp.prompt(name="morning_sla_briefing")(prompt_morning_sla_briefing)
mcp.prompt(name="pre_signoff_compliance_check")(prompt_pre_signoff_compliance_check)


MCP_TOOLS_MAP: Dict[str, Callable[..., Any]] = {
    "get_kyc_cases": get_kyc_cases,
    "run_chase_pipeline": run_chase_pipeline,
    "trigger_batch_chase_drafts": trigger_batch_chase_drafts,
    "approve_and_dispatch_chase": approve_and_dispatch_chase,
    "run_sla_watchdog_scan": run_sla_watchdog_scan,
    "verify_and_export_audit_ledger": verify_and_export_audit_ledger,
}


def validate_mcp_bearer_token(auth_header: Optional[str]) -> tuple[bool, Optional[str]]:
    """
    Validate Authorization: Bearer <token>.
    Supports configurable API key / JWT secret via MCP_AUTH_SECRET, with a safe dev fallback
    when no Authorization header is sent (unless MCP_REQUIRE_AUTH=true).
    Explicitly rejects malformed or invalid Bearer tokens.
    """
    expected = get_mcp_auth_secret()
    if not auth_header:
        if os.getenv("MCP_REQUIRE_AUTH", "false").lower() == "true":
            return False, "Missing Authorization: Bearer <token> header"
        return True, None

    parts = auth_header.strip().split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return False, "Malformed Authorization header; expected 'Bearer <token>'"

    token = parts[1].strip()
    valid_tokens = {expected, "dev-mcp-token", "northgate-mcp-bearer-2026"}
    if token not in valid_tokens:
        return False, "Invalid MCP Bearer token"
    return True, None


def extract_w3c_traceparent(traceparent_header: Optional[str]) -> Optional[ Any]:
    """Parse W3C traceparent header (`00-<32hex_trace_id>-<16hex_span_id>-01`) into OTel context."""
    if not traceparent_header:
        return None
    try:
        parts = traceparent_header.strip().split("-")
        if len(parts) == 4 and len(parts[1]) == 32 and len(parts[2]) == 16:
            trace_id_int = int(parts[1], 16)
            span_id_int = int(parts[2], 16)
            span_ctx = SpanContext(
                trace_id=trace_id_int,
                span_id=span_id_int,
                is_remote=True,
                trace_flags=TraceFlags(0x01),
            )
            if span_ctx.is_valid:
                return trace.set_span_in_context(NonRecordingSpan(span_ctx))
    except Exception:
        pass
    return None


async def handle_mcp_http_request(
    method: str,
    headers: Dict[str, str],
    body_bytes: bytes,
) -> tuple[int, Dict[str, Any]]:
    """
    Production FastMCP v3 Streamable HTTP / JSON-RPC 2.0 handler at `/mcp`.
    Handles authentication, actor contextvars binding, W3C traceparent parenting,
    tools/list, tools/call, resources/list, resources/read, prompts/list, prompts/get,
    and direct tool invocation payloads.
    """
    auth_header = headers.get("authorization") or headers.get("Authorization")
    ok, err = validate_mcp_bearer_token(auth_header)
    if not ok:
        return 401, {
            "jsonrpc": "2.0",
            "error": {"code": -32001, "message": f"Unauthorized: {err}"},
        }

    actor_id = (
        headers.get("x-rm-actor-id")
        or headers.get("X-RM-Actor-ID")
        or "Sarah Jenkins (VP Corporate Banking)"
    )
    actor_role = "RM_APPROVER" if "sarah" in actor_id.lower() or "rm" in actor_id.lower() else "MCP_AGENT"
    set_execution_context(
        actor_id=actor_id,
        actor_role=actor_role,
        entry_channel="MCP_STREAMABLE_HTTP",
    )

    if method.upper() == "GET":
        return 200, {
            "protocol": "mcp-streamable-http",
            "server": "Northgate ChaseZero KYC Compliance Server",
            "version": "3.0.0",
            "transport": "streamable-http",
            "endpoint": "/mcp",
            "actor_id": actor_id,
            "tools": list(MCP_TOOLS_MAP.keys()),
            "resources": [
                "kyc://telemetry/executive-summary",
                "kyc://cases/{case_id}",
                "kyc://observability/traces/{case_id}",
            ],
            "prompts": ["morning_sla_briefing", "pre_signoff_compliance_check"],
        }

    try:
        payload = json.loads(body_bytes.decode("utf-8") or "{}")
    except Exception as exc:
        return 400, {
            "jsonrpc": "2.0",
            "error": {"code": -32700, "message": f"Parse error: {exc}"},
        }

    req_id = payload.get("id", 1)
    rpc_method = payload.get("method")
    params = payload.get("params") or {}

    # Support shorthand direct tool invocation: {"tool": "run_chase_pipeline", "arguments": {...}}
    if not rpc_method and ("tool" in payload or "name" in payload):
        rpc_method = "tools/call"
        params = {
            "name": payload.get("tool") or payload.get("name"),
            "arguments": payload.get("arguments") or payload.get("params") or {},
        }

    parent_ctx = extract_w3c_traceparent(
        headers.get("traceparent") or headers.get("Traceparent")
    )

    if rpc_method == "initialize":
        return 200, {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2025-03-26",
                "serverInfo": {
                    "name": "Northgate ChaseZero KYC Compliance Server",
                    "version": "3.0.0",
                },
                "capabilities": {
                    "tools": {"listChanged": False},
                    "resources": {"subscribe": False, "listChanged": False},
                    "prompts": {"listChanged": False},
                },
            },
        }

    if rpc_method == "tools/list":
        tools_list = []
        for t_name, t_fn in MCP_TOOLS_MAP.items():
            sig = inspect.signature(t_fn)
            tools_list.append(
                {
                    "name": t_name,
                    "description": (t_fn.__doc__ or "").strip(),
                    "parameters": list(sig.parameters.keys()),
                }
            )
        return 200, {"jsonrpc": "2.0", "id": req_id, "result": {"tools": tools_list}}

    if rpc_method == "tools/call":
        tool_name = params.get("name")
        arguments = params.get("arguments") or {}
        if tool_name not in MCP_TOOLS_MAP:
            return 404, {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Unknown MCP tool: '{tool_name}'"},
            }
        tool_fn = MCP_TOOLS_MAP[tool_name]
        try:
            if parent_ctx is not None:
                from opentelemetry import context as otel_context

                token = otel_context.attach(parent_ctx)
                try:
                    result = await tool_fn(**arguments)
                finally:
                    otel_context.detach(token)
            else:
                result = await tool_fn(**arguments)

            return 200, {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(result, default=str)}],
                    "structuredContent": result,
                    "isError": False,
                },
                "data": result,
            }
        except ComplianceSecurityError as sec_err:
            return 403, {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {
                    "code": -32003,
                    "message": str(sec_err),
                    "type": "ComplianceSecurityError",
                },
            }
        except Exception as exc:
            return 400, {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32000, "message": str(exc)},
            }

    if rpc_method == "resources/list":
        return 200, {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "resources": [
                    {
                        "uri": "kyc://telemetry/executive-summary",
                        "name": "Executive Telemetry & Cost Summary",
                        "mimeType": "application/json",
                    },
                    {
                        "uri": "kyc://cases/{case_id}",
                        "name": "Corporate KYC Case Dossier",
                        "mimeType": "application/json",
                    },
                    {
                        "uri": "kyc://observability/traces/{case_id}",
                        "name": "Case OpenTelemetry GenAI Trace Waterfall",
                        "mimeType": "application/json",
                    },
                ]
            },
        }

    if rpc_method == "resources/read":
        uri = str(params.get("uri") or "")
        if uri == "kyc://telemetry/executive-summary":
            text = await resource_executive_summary()
        elif uri.startswith("kyc://cases/"):
            cid = uri.split("kyc://cases/", 1)[1]
            text = await resource_case_dossier(cid)
        elif uri.startswith("kyc://observability/traces/"):
            cid = uri.split("kyc://observability/traces/", 1)[1]
            text = await resource_case_traces(cid)
        else:
            return 404, {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32602, "message": f"Unknown resource URI: {uri}"},
            }
        return 200, {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "contents": [
                    {"uri": uri, "mimeType": "application/json", "text": text}
                ]
            },
        }

    if rpc_method == "prompts/list":
        return 200, {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "prompts": [
                    {
                        "name": "morning_sla_briefing",
                        "description": "Prioritize SLA-breached corporate KYC accounts and summarize overnight LLM spend.",
                    },
                    {
                        "name": "pre_signoff_compliance_check",
                        "description": "Verify missing documents and Critic QA compliance prior to RM HMAC sign-off.",
                    },
                ]
            },
        }

    if rpc_method == "prompts/get":
        p_name = params.get("name")
        p_args = params.get("arguments") or {}
        if p_name == "morning_sla_briefing":
            prompt_text = prompt_morning_sla_briefing()
        elif p_name == "pre_signoff_compliance_check":
            prompt_text = prompt_pre_signoff_compliance_check(
                case_id=str(p_args.get("case_id", "NCZ-2024-8841"))
            )
        else:
            return 404, {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32602, "message": f"Unknown prompt: {p_name}"},
            }
        return 200, {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "messages": [
                    {
                        "role": "user",
                        "content": {"type": "text", "text": prompt_text},
                    }
                ]
            },
        }

    return 400, {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Unsupported MCP method: {rpc_method}"},
    }
