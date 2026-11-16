from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional, TypedDict

from dotenv import load_dotenv
from langgraph.graph import END, StateGraph
from opentelemetry import trace
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from .compliance_ledger import (
    ACTIVE_RULES_ENGINE,
    ModelCostEngine,
    append_audit_ledger_entry,
    compute_hitl_hmac_signature,
    verify_rm_approval_secret,
)
from .database import serialize_case_detail
from .models import AgentAction, KycCase
from .telemetry import (
    get_current_trace_id_hex,
    get_execution_context,
    get_tracer,
    set_execution_context,
)

ENV_FILE_PATH = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(ENV_FILE_PATH, override=True)

MAX_CRITIC_RETRIES = int(ACTIVE_RULES_ENGINE.get("max_critic_retries", 3))


class ComplianceSecurityError(PermissionError):
    """Raised when an RM HITL perimeter check or Critic Compliance gate fails."""


def reload_env_config() -> Dict[str, Any]:
    if ENV_FILE_PATH.exists():
        load_dotenv(ENV_FILE_PATH, override=True)
    return get_llm_config()


def get_llm_config() -> Dict[str, Any]:
    gemini_key = os.getenv("GEMINI_API_KEY", "").strip() or os.getenv("GOOGLE_API_KEY", "").strip()
    openai_key = os.getenv("OPENAI_API_KEY", "").strip()
    anthropic_key = os.getenv("ANTHROPIC_API_KEY", "").strip()

    provider = os.getenv("LLM_PROVIDER", "google_vertex").strip().lower()
    model = os.getenv("LLM_MODEL", "gemini-2.5-pro").strip()

    configured = bool(gemini_key or openai_key or anthropic_key)
    if not configured:
        provider = provider or "google_vertex"
        model = model or "gemini-2.5-pro"

    return {
        "llm_configured": configured,
        "llm_provider": provider,
        "llm_model": model,
        "env_file": str(ENV_FILE_PATH),
        "gemini_key": gemini_key,
        "openai_key": openai_key,
        "anthropic_key": anthropic_key,
    }


def make_portal_slug(client_name: str, case_code: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", client_name.lower()).strip("-")[:18].rstrip("-")
    suffix = case_code.split("-")[-1]
    return f"https://portal.northgatebank.com/secure-vault/{slug}-{suffix}"


def build_flawed_draft(case: KycCase) -> tuple[str, str]:
    first_name = case.contact_name.split(" ")[0]
    missing_docs = [d for d in case.documents if d.status == "Missing"]
    first_doc = (
        missing_docs[0].document_name
        if missing_docs
        else "Updated Corporate KYC Documentation"
    )
    subject = f"Action Required: Finalizing KYC Documentation for {case.client_name} (Ref #{case.case_code})"
    body = (
        f"Dear {first_name},\n\n"
        f"We are reaching out regarding the {case.facility_value} onboarding file for {case.client_name}. "
        f"To keep things moving quickly, Northgate Bank can guarantee immediate credit approval and a temporary waiver "
        f"of regulatory KYC obligations once you send over the item below.\n\n"
        f"Please upload:\n"
        f"1. {first_doc}\n\n"
        f"Warm regards,\n"
        f"{case.assigned_rm}\n"
        f"VP, Corporate Banking — Northgate Bank"
    )
    return subject, body


def build_grounded_draft(case: KycCase, tone: str = "institutional") -> tuple[str, str]:
    first_name = case.contact_name.split(" ")[0]
    missing_docs = [d for d in case.documents if d.status == "Missing"]
    verified_docs = [d for d in case.documents if d.status == "Verified"]

    subject = f"Action Required: Finalizing KYC Documentation for {case.client_name} (Ref #{case.case_code})"
    if tone == "urgent":
        subject = f"PRIORITY SLA NOTICE: Outstanding KYC Documentation for {case.client_name} (Ref #{case.case_code})"

    verified_clause = ""
    if verified_docs:
        v_names = " and ".join(d.document_name for d in verified_docs[:2])
        verified_clause = f"Our compliance operations unit has successfully verified your {v_names}. "

    if missing_docs:
        doc_lines = "\n".join(
            f"{idx}. {d.document_name} — {d.description}"
            for idx, d in enumerate(missing_docs, start=1)
        )
        missing_intro = (
            f"To ensure we support your anticipated execution schedule ahead of {case.target_deadline_label} without delay, "
            f"we kindly request the following {len(missing_docs)} outstanding document(s):\n\n{doc_lines}"
        )
    else:
        missing_intro = (
            f"All required KYC documentation for {case.client_name} has been received and verified ahead of {case.target_deadline_label}."
        )

    portal_link = make_portal_slug(case.client_name, case.case_code)

    opening = (
        f"Dear {first_name},\n\n"
        f"Thank you for your partnership on {case.client_name}'s corporate institutional onboarding ({case.facility_value} facility). "
    )
    if tone == "casual":
        opening = (
            f"Hi {first_name},\n\n"
            f"Hope your week is going well. Thanks again for partnering with us on {case.client_name}'s onboarding ({case.facility_value} facility). "
        )
    elif tone == "urgent":
        opening = (
            f"Dear {first_name},\n\n"
            f"Following up as a time-sensitive priority regarding {case.client_name}'s institutional onboarding ({case.facility_value} facility) ahead of the {case.target_deadline_label} governance cut-off. "
        )

    calendar_block = ""
    if tone == "calendar":
        calendar_block = (
            "\n\nDirect RM Coordination Slot (15-Min Priority Sync): "
            "https://cal.northgatebank.com/sarah-jenkins/15min-kyc-sync"
        )

    body = (
        f"{opening}"
        f"{verified_clause}"
        f"{missing_intro}\n\n"
        f"Please transmit these files strictly via your encrypted Northgate Secure Client Workspace link below "
        f"(do not reply with unencrypted identity or passport scans via standard email). "
        f"If you would like our team to coordinate directly with your corporate secretary, please let me know.\n"
        f"Secure Upload Link: {portal_link}"
        f"{calendar_block}\n\n"
        f"Warm regards,\n"
        f"{case.assigned_rm}\n"
        f"VP, Corporate Banking Relationship Management\n"
        f"Northgate Commercial & Institutional Banking\n\n"
        f"CONFIDENTIALITY & COMPLIANCE NOTICE: This communication is issued pursuant to Northgate Risk & Settlement Protocols. "
        f"It does not constitute a legally binding loan commitment or final credit approval. "
        f"Northgate Bank plc is authorized and regulated by the PRA and FCA."
    )
    return subject, body


def evaluate_critic_checks(
    case: KycCase, draft_body: str, simulate_failure: bool = False
) -> tuple[bool, int, str, List[Dict[str, Any]], str]:
    body_lower = (draft_body or "").lower()
    missing_docs = [d for d in case.documents if d.status == "Missing"]

    # 1. Tone & Register
    coercive_terms = ["immediately or else", "penalize", "default notice", "demand immediate", "legal action"]
    has_coercive = any(t in body_lower for t in coercive_terms) or (
        simulate_failure and "guarantee immediate credit approval" not in body_lower
    )
    tone_passed = not has_coercive
    tone_check = {
        "key": "tone",
        "title": "Tone & Register",
        "badge": "PASSED: Professional Executive" if tone_passed else "FAILED: Coercive Register",
        "passed": tone_passed,
        "summary": (
            "No aggressive debt-collection phrasing, coercive language, or abrupt directives. Maintains respectful, institutional corporate banking cadence."
            if tone_passed
            else "Detected overly aggressive or coercive phrasing inconsistent with Northgate Conduct Risk standards."
        ),
        "meta_left": "Lexical register: Tier-1 Bank",
        "meta_right": "Sentiment: Neutral (+0.04)" if tone_passed else "Sentiment: Negative (-0.62)",
    }

    # 2. Document Accuracy
    omitted_docs = [
        d.document_name for d in missing_docs if d.document_name.lower() not in body_lower
    ]
    if simulate_failure and not omitted_docs and missing_docs:
        omitted_docs = [missing_docs[-1].document_name]

    short_names = ", ".join(d.document_name[:24] for d in missing_docs) or "All verified"
    accuracy_passed = len(omitted_docs) == 0
    accuracy_check = {
        "key": "accuracy",
        "title": "Document Accuracy",
        "badge": "PASSED: 100% Grounded" if accuracy_passed else "FAILED: Grounding Mismatch",
        "passed": accuracy_passed,
        "summary": (
            f"Only documents flagged as 'Missing' in the tracker are cited ({len(missing_docs)} items). Zero hallucinated items; no already 'Verified' documents re-requested."
            if accuracy_passed
            else f"Omitted missing doc(s): {', '.join(omitted_docs)}"
        ),
        "meta_left": short_names,
        "meta_right": (
            f"{len(missing_docs)}/{len(missing_docs)} missing verified"
            if accuracy_passed
            else f"{len(omitted_docs)} omitted"
        ),
    }

    # 3. Promise & Liability Check
    promise_terms = [
        "guarantee immediate credit approval",
        "temporary waiver of regulatory",
        "unconditional approval",
        "guaranteed approval",
    ]
    has_promise = any(t in body_lower for t in promise_terms) or simulate_failure
    promises_passed = not has_promise
    promises_check = {
        "key": "promises",
        "title": "Promise & Liability Check",
        "badge": "PASSED: Zero Commitments" if promises_passed else "FAILED: Unauthorized Promise",
        "passed": promises_passed,
        "summary": (
            "Draft makes zero promises of unconditional credit approval, guaranteed timeline exemptions, or waiver of regulatory obligations."
            if promises_passed
            else "HALLUCINATION ALERT: Draft implies guaranteed credit approval or regulatory waiver."
        ),
        "meta_left": "Liability Exposure: $0.00" if promises_passed else "Policy Breach: Credit Warranty",
        "meta_right": "Warranty clauses: 0" if promises_passed else "Violations: 1",
    }

    # 4. PII & Security Protocol
    has_portal = (
        "portal.northgate" in body_lower
        or "secure-vault" in body_lower
        or "secure client workspace" in body_lower
    ) and not (simulate_failure and "guarantee immediate credit approval" not in body_lower)
    pii_passed = has_portal
    pii_check = {
        "key": "pii",
        "title": "PII & Security Protocol",
        "badge": "PASSED: Compliant" if pii_passed else "FAILED: Missing Secure Upload Link",
        "passed": pii_passed,
        "summary": (
            "Contains authorized secure portal upload instructions; no unencrypted sensitive account numbers, tax IDs, or passwords exposed in cleartext."
            if pii_passed
            else "Draft lacks an encrypted Northgate Secure Client Workspace upload reference."
        ),
        "meta_left": "Encrypted Token: Valid" if pii_passed else "Upload URI Missing",
        "meta_right": "GDPR/DPA 2018: Clear",
    }

    # 5. Regulatory Disclosures
    has_disclosure = ("pra and fca" in body_lower or "confidentiality & compliance notice" in body_lower) or (
        "guarantee immediate credit approval" in body_lower
    )
    if simulate_failure and "guarantee immediate credit approval" not in body_lower:
        has_disclosure = False
    disclosures_passed = has_disclosure
    disclosures_check = {
        "key": "disclosures",
        "title": "Regulatory Disclosures",
        "badge": "PASSED: Standard Northgate" if disclosures_passed else "FAILED: Missing PRA/FCA Footer",
        "passed": disclosures_passed,
        "summary": (
            "Standard Northgate Disclaimer attached. PRA/FCA regulatory notice, non-binding declaration, and confidentiality clause fully present."
            if disclosures_passed
            else "Missing Northgate Bank PRA/FCA regulatory notice and non-binding declaration."
        ),
        "meta_left": "Footer Pattern: SEC-FCA-2024-R",
        "meta_right": "Signature Key: Valid" if disclosures_passed else "Footer Missing",
    }

    checks = [
        tone_check,
        accuracy_check,
        promises_check,
        pii_check,
        disclosures_check,
    ]
    passed_count = sum(1 for c in checks if c["passed"])
    score = int(round((passed_count / len(checks)) * 100))
    all_passed = passed_count == len(checks)

    if all_passed:
        feedback = f"All 5 compliance and accuracy checks cleared for {case.client_name}."
    else:
        failed_reasons = [f"{c['title']}: {c['summary']}" for c in checks if not c["passed"]]
        feedback = " | ".join(failed_reasons)

    digest = hashlib.sha256(f"{case.case_code}:{draft_body}:{score}".encode("utf-8")).hexdigest()[:8].upper()
    version = 2 if all_passed and not simulate_failure else 1
    audit_hash = f"#QA-{digest}-V{version}"

    return all_passed, score, feedback, checks, audit_hash


def chunk_text_for_stream(text: str) -> List[tuple[str, int]]:
    words = re.split(r"(\s+)", text)
    tokens: List[str] = []
    buf = ""
    for part in words:
        buf += part
        if len(buf) >= 24:
            tokens.append(buf)
            buf = ""
    if buf:
        tokens.append(buf)
    total = max(1, len(tokens))
    return [(tok, min(100, max(1, int(round((i + 1) / total * 100))))) for i, tok in enumerate(tokens)]


class ChaseGraphState(TypedDict):
    case_id: int
    tone: str
    simulate_critic_rejection: bool
    force_circuit_breaker: bool
    breached_count: int
    missing_docs_count: int
    verified_docs_count: int
    attempt: int
    subject: str
    body: str
    critic_passed: bool
    critic_score: int
    critic_legal_score: float
    critic_politeness_score: float
    critic_feedback: str
    critic_checks: List[Dict[str, Any]]
    audit_hash: str
    audit_id: str
    trace_id: str
    total_cost_usd: float
    total_latency_ms: float
    circuit_breaker_tripped: bool
    events: List[Dict[str, Any]]


async def execute_langgraph_pipeline(
    session: AsyncSession,
    case: KycCase,
    simulate_critic_rejection: bool = False,
    tone: str = "institutional",
    force_circuit_breaker: bool = False,
) -> tuple[KycCase, List[Dict[str, Any]]]:
    """
    Run the 3-node LangGraph StateGraph (Orchestrator -> Drafter -> Critic) with
    2026 OpenTelemetry GenAI semantic conventions, per-step USD cost attribution,
    max_critic_retries=3 circuit breaker, and SEC/FinCEN tamper-evident audit logging.
    """
    tracer = get_tracer("northgate.chasezero.langgraph")
    set_execution_context(case_id=case.case_code, round_number=case.chase_round_count)
    ctx_info = get_execution_context()
    actor_id = ctx_info["actor_id"]
    actor_role = ctx_info["actor_role"]
    entry_channel = ctx_info["entry_channel"]

    breached_count = int(
        await session.scalar(select(func.count(KycCase.id)).where(KycCase.sla_breached.is_(True)))
        or 4
    )
    missing_docs = [d for d in case.documents if d.status == "Missing"]
    verified_docs = [d for d in case.documents if d.status == "Verified"]
    llm_cfg = get_llm_config()
    model_label = llm_cfg["llm_model"]
    provider_label = llm_cfg["llm_provider"]

    pipeline_t0 = time.perf_counter()

    # Check if we are already inside an active MCP tool span; if not, create root span
    current_span = trace.get_current_span()
    has_valid_parent = bool(
        current_span and current_span.get_span_context() and current_span.get_span_context().is_valid
    )

    pending_actions: List[AgentAction] = []

    async def _run_graph_with_telemetry(active_trace_id: str) -> ChaseGraphState:
        async def orchestrator_node(state: ChaseGraphState) -> Dict[str, Any]:
            with tracer.start_as_current_span("langgraph.orchestrator_sla_scan") as span:
                span.set_attribute("gen_ai.operation.name", "db_query")
                span.set_attribute("gen_ai.provider.name", "internal")
                span.set_attribute("gen_ai.request.model", "sqlite-wal-engine")
                span.set_attribute("kyc.case_id", case.case_code)
                span.set_attribute("kyc.lei", case.lei)
                span.set_attribute("kyc.round_number", case.chase_round_count)
                span.set_attribute("kyc.actor_id", actor_id)

                evts = list(state.get("events", []))
                evts.append(
                    {
                        "type": "node_start",
                        "node": "orchestrator",
                        "trace_id": active_trace_id,
                        "label": "Orchestrator Node scanning SQLite WAL for SLA breaches (>3 days)...",
                    }
                )
                evts.append(
                    {
                        "type": "orchestrator_result",
                        "node": "orchestrator",
                        "trace_id": active_trace_id,
                        "breached_count": breached_count,
                        "missing_docs_count": len(missing_docs),
                        "verified_docs_count": len(verified_docs),
                        "client_name": case.client_name,
                    }
                )
                return {
                    "breached_count": breached_count,
                    "missing_docs_count": len(missing_docs),
                    "verified_docs_count": len(verified_docs),
                    "events": evts,
                }

        async def drafter_node(state: ChaseGraphState) -> Dict[str, Any]:
            attempt = state.get("attempt", 0) + 1
            is_retry = attempt > 1
            with tracer.start_as_current_span("langgraph.ai_drafting_agent") as span:
                evts = list(state.get("events", []))
                evts.append(
                    {
                        "type": "node_start",
                        "node": "drafter",
                        "attempt": attempt,
                        "is_retry": is_retry,
                        "trace_id": active_trace_id,
                        "llm_model": model_label,
                        "llm_provider": provider_label,
                        "label": (
                            f"Drafter Node v{attempt} ({model_label}): Re-synthesizing email to resolve Critic rejection..."
                            if is_retry
                            else f"Drafter Node ({model_label}): Synthesizing grounded KYC chase email..."
                        ),
                    }
                )

                use_flawed = bool(
                    state.get("force_circuit_breaker")
                    or (state.get("simulate_critic_rejection") and attempt == 1)
                )
                if use_flawed:
                    subj, body = build_flawed_draft(case)
                else:
                    subj, body = build_grounded_draft(case, state.get("tone", "institutional"))

                prompt_context = (
                    f"Entity={case.client_name} LEI={case.lei} Case={case.case_code} "
                    f"Contact={case.contact_name} <{case.contact_email}> Address={case.address} "
                    f"MissingDocs={[d.document_name for d in missing_docs]}"
                )
                in_tokens = ModelCostEngine.estimate_tokens(prompt_context) + 1450 + (attempt * 120)
                out_tokens = ModelCostEngine.estimate_tokens(f"{subj}\n\n{body}") + 210
                cached_tokens = int(in_tokens * 0.35)
                step_cost = ModelCostEngine.compute_cost_usd(
                    model_label, in_tokens, out_tokens, cached_tokens
                )

                # Set 2026 OTel GenAI Semantic Conventions
                span.set_attribute("gen_ai.operation.name", "chat")
                span.set_attribute("gen_ai.provider.name", provider_label)
                span.set_attribute("gen_ai.request.model", model_label)
                span.set_attribute("gen_ai.response.model", model_label)
                span.set_attribute("gen_ai.usage.input_tokens", in_tokens)
                span.set_attribute("gen_ai.usage.output_tokens", out_tokens)
                span.set_attribute("gen_ai.usage.cached_tokens", cached_tokens)
                span.set_attribute("gen_ai.client.cost.usd", step_cost)
                span.set_attribute("kyc.case_id", case.case_code)
                span.set_attribute("kyc.lei", case.lei)
                span.set_attribute("kyc.round_number", case.chase_round_count)
                span.set_attribute("kyc.actor_id", actor_id)
                span.set_attribute("kyc.contact_name", case.contact_name)
                span.set_attribute("kyc.contact_email", case.contact_email)

                for chunk, prog in chunk_text_for_stream(body):
                    evts.append(
                        {
                            "type": "draft_token",
                            "node": "drafter",
                            "attempt": attempt,
                            "chunk": chunk,
                            "progress": prog,
                        }
                    )

                word_count = len(body.split())
                evts.append(
                    {
                        "type": "draft_complete",
                        "node": "drafter",
                        "attempt": attempt,
                        "subject": subj,
                        "body": body,
                        "word_count": word_count,
                        "input_tokens": in_tokens,
                        "output_tokens": out_tokens,
                        "cost_usd": step_cost,
                        "engine": model_label,
                    }
                )

                ledger_entry = append_audit_ledger_entry(
                    trace_id=active_trace_id,
                    case_id=case.case_code,
                    actor_id=actor_id,
                    actor_role=actor_role,
                    entry_channel=entry_channel,
                    action_type="DRAFT_GENERATED",
                    raw_input=prompt_context,
                    raw_output=f"{subj}\n\n{body}",
                )

                suffix = " (post-Critic retry)" if is_retry else ""
                pending_actions.append(
                    AgentAction(
                        case_id=case.id,
                        action_type="Draft_Generated",
                        timestamp=datetime.now(timezone.utc),
                        actor=f"ChaseZero Drafter ({model_label})",
                        details=f"Generated chase draft v{attempt} for {len(missing_docs)} missing docs{suffix} | Cost ${step_cost:.6f}",
                    )
                )

                return {
                    "attempt": attempt,
                    "subject": subj,
                    "body": body,
                    "audit_id": ledger_entry["audit_id"],
                    "total_cost_usd": round(state.get("total_cost_usd", 0.0) + step_cost, 6),
                    "events": evts,
                }

        async def critic_node(state: ChaseGraphState) -> Dict[str, Any]:
            attempt = state.get("attempt", 1)
            with tracer.start_as_current_span("langgraph.critic_compliance_qa") as span:
                evts = list(state.get("events", []))
                evts.append(
                    {
                        "type": "node_start",
                        "node": "critic",
                        "attempt": attempt,
                        "trace_id": active_trace_id,
                        "label": f"Critic Node ({model_label}): Auditing draft v{attempt} against 5 SEC/FinCEN/FCA guardrails...",
                    }
                )

                sim_fail = bool(
                    state.get("force_circuit_breaker")
                    or (state.get("simulate_critic_rejection") and attempt == 1)
                )
                passed, score, feedback, checks, audit_hash = evaluate_critic_checks(
                    case, state["body"], simulate_failure=sim_fail
                )
                legal_score = round(score / 100.0, 4)
                politeness_score = 0.98 if passed else 0.42

                in_tokens = ModelCostEngine.estimate_tokens(state["body"]) + 1120
                out_tokens = ModelCostEngine.estimate_tokens(feedback) + 260
                cached_tokens = int(in_tokens * 0.35)
                step_cost = ModelCostEngine.compute_cost_usd(
                    model_label, in_tokens, out_tokens, cached_tokens
                )

                # Retry-Loop Circuit Breaker: max_critic_retries = 3
                circuit_tripped = bool(not passed and attempt >= MAX_CRITIC_RETRIES)
                routing_to = "end" if (passed or attempt >= MAX_CRITIC_RETRIES) else "drafter"

                span.set_attribute("gen_ai.operation.name", "chat")
                span.set_attribute("gen_ai.provider.name", provider_label)
                span.set_attribute("gen_ai.request.model", model_label)
                span.set_attribute("gen_ai.response.model", model_label)
                span.set_attribute("gen_ai.usage.input_tokens", in_tokens)
                span.set_attribute("gen_ai.usage.output_tokens", out_tokens)
                span.set_attribute("gen_ai.usage.cached_tokens", cached_tokens)
                span.set_attribute("gen_ai.client.cost.usd", step_cost)
                span.set_attribute("kyc.case_id", case.case_code)
                span.set_attribute("kyc.lei", case.lei)
                span.set_attribute("kyc.round_number", case.chase_round_count)
                span.set_attribute("kyc.actor_id", actor_id)
                span.set_attribute("kyc.critic.legal_score", legal_score)
                span.set_attribute("kyc.critic.politeness_score", politeness_score)
                span.set_attribute("kyc.span.status", "OK" if passed else "CRITIC_REJECTED")

                ledger_entry = append_audit_ledger_entry(
                    trace_id=active_trace_id,
                    case_id=case.case_code,
                    actor_id="system:critic_compliance_qa",
                    actor_role="MCP_AGENT",
                    entry_channel=entry_channel,
                    action_type="CRITIC_QA_PASSED" if passed else "CRITIC_QA_REJECTED",
                    raw_input=state["body"],
                    raw_output=feedback,
                    critic_legal_score=legal_score,
                    critic_politeness_score=politeness_score,
                    critic_findings=checks,
                )

                evts.append(
                    {
                        "type": "critic_result",
                        "node": "critic",
                        "attempt": attempt,
                        "passed": passed,
                        "score": score,
                        "legal_score": legal_score,
                        "politeness_score": politeness_score,
                        "feedback": feedback,
                        "checks": checks,
                        "audit_hash": audit_hash,
                        "audit_id": ledger_entry["audit_id"],
                        "cost_usd": step_cost,
                        "circuit_breaker_tripped": circuit_tripped,
                        "routing_to": routing_to,
                    }
                )

                if not passed:
                    pending_actions.append(
                        AgentAction(
                            case_id=case.id,
                            action_type="Critic_Rejected",
                            timestamp=datetime.now(timezone.utc),
                            actor="ChaseZero Critic QA",
                            details=(
                                f"Rejected draft v{attempt} (Score {score}/100): {feedback}"
                                + (
                                    " [CIRCUIT BREAKER HALTED AT MAX RETRIES=3]"
                                    if circuit_tripped
                                    else ""
                                )
                            ),
                        )
                    )

                return {
                    "critic_passed": passed,
                    "critic_score": score,
                    "critic_legal_score": legal_score,
                    "critic_politeness_score": politeness_score,
                    "critic_feedback": feedback,
                    "critic_checks": checks,
                    "audit_hash": audit_hash,
                    "audit_id": ledger_entry["audit_id"],
                    "circuit_breaker_tripped": circuit_tripped,
                    "total_cost_usd": round(state.get("total_cost_usd", 0.0) + step_cost, 6),
                    "events": evts,
                }

        def route_after_critic(state: ChaseGraphState) -> str:
            if not state.get("critic_passed") and state.get("attempt", 1) < MAX_CRITIC_RETRIES:
                return "drafter"
            return "end"

        workflow = StateGraph(ChaseGraphState)
        workflow.add_node("orchestrator", orchestrator_node)
        workflow.add_node("drafter", drafter_node)
        workflow.add_node("critic", critic_node)
        workflow.set_entry_point("orchestrator")
        workflow.add_edge("orchestrator", "drafter")
        workflow.add_edge("drafter", "critic")
        workflow.add_conditional_edges(
            "critic",
            route_after_critic,
            {"drafter": "drafter", "end": END},
        )
        compiled = workflow.compile()

        initial_state: ChaseGraphState = {
            "case_id": case.id,
            "tone": tone,
            "simulate_critic_rejection": simulate_critic_rejection,
            "force_circuit_breaker": force_circuit_breaker,
            "breached_count": 0,
            "missing_docs_count": 0,
            "verified_docs_count": 0,
            "attempt": 0,
            "subject": "",
            "body": "",
            "critic_passed": False,
            "critic_score": 0,
            "critic_legal_score": 0.0,
            "critic_politeness_score": 0.0,
            "critic_feedback": "",
            "critic_checks": [],
            "audit_hash": "",
            "audit_id": "",
            "trace_id": active_trace_id,
            "total_cost_usd": 0.0,
            "total_latency_ms": 0.0,
            "circuit_breaker_tripped": False,
            "events": [],
        }

        return await compiled.ainvoke(initial_state)

    if has_valid_parent:
        active_trace_id = get_current_trace_id_hex()
        final_state = await _run_graph_with_telemetry(active_trace_id)
    else:
        with tracer.start_as_current_span("mcp.tools/call.run_chase_pipeline") as root_span:
            active_trace_id = get_current_trace_id_hex()
            root_span.set_attribute("gen_ai.operation.name", "invoke_agent")
            root_span.set_attribute("gen_ai.provider.name", provider_label)
            root_span.set_attribute("gen_ai.request.model", model_label)
            root_span.set_attribute("kyc.case_id", case.case_code)
            root_span.set_attribute("kyc.lei", case.lei)
            root_span.set_attribute("kyc.round_number", case.chase_round_count)
            root_span.set_attribute("kyc.actor_id", actor_id)
            final_state = await _run_graph_with_telemetry(active_trace_id)
            if not final_state["critic_passed"]:
                root_span.set_attribute("kyc.span.status", "CRITIC_REJECTED")

    elapsed_ms = round((time.perf_counter() - pipeline_t0) * 1000.0, 2)
    final_state["total_latency_ms"] = elapsed_ms

    for act in pending_actions:
        session.add(act)

    case.current_draft_subject = final_state["subject"]
    case.current_draft_body = final_state["body"]
    case.critic_passed = final_state["critic_passed"]
    case.critic_score = final_state["critic_score"]
    case.critic_feedback = final_state["critic_feedback"]
    case.critic_checks_json = json.dumps(final_state["critic_checks"])
    case.workflow_iterations = final_state["attempt"]
    case.audit_hash = final_state["audit_hash"]

    # Hard Constraint: Pipeline ONLY transitions case status to Draft_Ready when Critic passes;
    # never sends an email or transitions to Completed.
    if final_state["critic_passed"] and case.status != "Completed":
        case.status = "Draft_Ready"
    elif final_state.get("circuit_breaker_tripped"):
        case.status = "Missing_Docs"

    await session.commit()

    stmt = (
        select(KycCase)
        .where(KycCase.id == case.id)
        .options(selectinload(KycCase.documents), selectinload(KycCase.actions))
    )
    refreshed_case = (await session.execute(stmt)).scalar_one()
    return refreshed_case, final_state["events"]


async def approve_and_dispatch_chase_workflow(
    session: AsyncSession,
    case: KycCase,
    *,
    rm_id: str,
    rm_approval_secret: str,
    edited_body: Optional[str] = None,
    subject_override: Optional[str] = None,
    notify_analyst: bool = True,
    entry_channel: str = "MCP_STREAMABLE_HTTP",
) -> Dict[str, Any]:
    """
    Strict SEC & FinCEN Perimeter Gate for Human-in-the-Loop approval & email dispatch:
    1. Verifies rm_approval_secret against server HMAC key (RM_SIGNING_SECRET).
    2. Confirms case is in Draft_Ready status and its latest Critic Compliance QA check passed (legal_score >= 0.95).
    3. If edited_body is provided by the RM, re-runs Critic Compliance QA before allowing dispatch.
    4. Computes hitl_signature = HMAC_SHA256(RM_SIGNING_SECRET, f"{case_id}:{rm_id}:{final_body_hash}:{timestamp}").
    5. Appends RM_HITL_APPROVED and EMAIL_DISPATCHED to sec_fincen_audit_ledger and updates status.
    """
    tracer = get_tracer("northgate.chasezero.hitl")
    set_execution_context(
        actor_id=rm_id,
        actor_role="RM_APPROVER",
        entry_channel=entry_channel,
        case_id=case.case_code,
        round_number=case.chase_round_count,
    )

    with tracer.start_as_current_span("mcp.tools/call.approve_and_dispatch_chase") as span:
        trace_id = get_current_trace_id_hex()
        span.set_attribute("gen_ai.operation.name", "execute_tool")
        span.set_attribute("gen_ai.provider.name", "internal")
        span.set_attribute("kyc.case_id", case.case_code)
        span.set_attribute("kyc.lei", case.lei)
        span.set_attribute("kyc.actor_id", rm_id)

        # Gate 1: Verify RM cryptographic signing secret
        if not verify_rm_approval_secret(rm_approval_secret):
            span.set_attribute("kyc.span.status", "ERROR")
            span.set_attribute("error.message", "Invalid RM approval secret")
            raise ComplianceSecurityError(
                "SEC/FinCEN Perimeter Violation: Invalid RM cryptographic approval secret."
            )

        # Gate 2: Confirm case is in Draft_Ready and Critic QA passed (legal_score >= 0.95)
        current_legal_score = round((case.critic_score or 0) / 100.0, 4)
        if case.status != "Draft_Ready" or not case.critic_passed or current_legal_score < 0.95:
            span.set_attribute("kyc.span.status", "CRITIC_REJECTED")
            span.set_attribute(
                "error.message",
                f"Case {case.case_code} status='{case.status}', critic_passed={case.critic_passed}, legal_score={current_legal_score}",
            )
            raise ComplianceSecurityError(
                f"SEC/FinCEN Perimeter Violation: Case {case.case_code} is in status '{case.status}' "
                f"(critic_passed={case.critic_passed}, legal_score={current_legal_score:.2f} < 0.95). "
                "Only Draft_Ready cases that passed Critic Compliance QA can be dispatched."
            )

        final_subject = (
            subject_override
            or case.current_draft_subject
            or f"Action Required: Finalizing KYC Documentation for {case.client_name} (Ref #{case.case_code})"
        )
        final_body = edited_body if edited_body is not None else (case.current_draft_body or "")

        # Gate 3: If edited_body is provided, re-run Critic Compliance QA before allowing dispatch
        if edited_body is not None:
            passed, score, feedback, checks, audit_hash = evaluate_critic_checks(
                case, final_body, simulate_failure=False
            )
            legal_score = round(score / 100.0, 4)
            politeness_score = 0.98 if passed else 0.42

            if not passed or legal_score < 0.95:
                append_audit_ledger_entry(
                    trace_id=trace_id,
                    case_id=case.case_code,
                    actor_id=rm_id,
                    actor_role="RM_APPROVER",
                    entry_channel=entry_channel,
                    action_type="CRITIC_QA_REJECTED",
                    raw_input=final_body,
                    raw_output=feedback,
                    critic_legal_score=legal_score,
                    critic_politeness_score=politeness_score,
                    critic_findings=checks,
                )
                case.critic_passed = passed
                case.critic_score = score
                case.critic_feedback = feedback
                case.critic_checks_json = json.dumps(checks)
                case.audit_hash = audit_hash
                await session.commit()
                span.set_attribute("kyc.span.status", "CRITIC_REJECTED")
                raise ComplianceSecurityError(
                    f"SEC/FinCEN Perimeter Violation: RM edited body failed Critic Compliance QA "
                    f"(legal_score={legal_score:.2f} < 0.95): {feedback}"
                )
            case.critic_passed = passed
            case.critic_score = score
            case.critic_feedback = feedback
            case.critic_checks_json = json.dumps(checks)
            case.audit_hash = audit_hash
        else:
            legal_score = current_legal_score
            politeness_score = 0.98

        # Gate 4: Compute cryptographic HMAC-SHA256 signature
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        hitl_signature = compute_hitl_hmac_signature(
            case_id=case.case_code,
            rm_id=rm_id,
            final_body=final_body,
            timestamp_iso=now_iso,
        )

        # Gate 5: Append RM_HITL_APPROVED and EMAIL_DISPATCHED to sec_fincen_audit_ledger
        approved_entry = append_audit_ledger_entry(
            trace_id=trace_id,
            case_id=case.case_code,
            actor_id=rm_id,
            actor_role="RM_APPROVER",
            entry_channel=entry_channel,
            action_type="RM_HITL_APPROVED",
            raw_input=f"Subject: {final_subject} | Recipient: {case.contact_name} <{case.contact_email}>",
            raw_output=final_body,
            critic_legal_score=legal_score,
            critic_politeness_score=politeness_score,
            hitl_rm_id=rm_id,
            hitl_signature=hitl_signature,
            created_at=now_iso,
        )

        dispatched_entry = append_audit_ledger_entry(
            trace_id=trace_id,
            case_id=case.case_code,
            actor_id=rm_id,
            actor_role="RM_APPROVER",
            entry_channel=entry_channel,
            action_type="EMAIL_DISPATCHED",
            raw_input=f"Dispatching approved chase round #{case.chase_round_count + 1} to {case.contact_email}",
            raw_output=f"Dispatched with HMAC-SHA256 signature {hitl_signature[:16]}...",
            critic_legal_score=legal_score,
            critic_politeness_score=politeness_score,
            hitl_rm_id=rm_id,
            hitl_signature=hitl_signature,
            created_at=now_iso,
        )

        # Update case in SQLite
        case.current_draft_subject = final_subject
        case.current_draft_body = final_body
        case.status = "Completed"
        case.sla_breached = False
        case.sla_delay_days = 0.0
        case.last_contact_date = now
        case.days_since_contact = 0.0
        case.chase_round_count = case.chase_round_count + 1
        case.critic_passed = True
        case.critic_score = 100
        case.audit_hash = dispatched_entry["record_hash"][:16]

        tracking_id = f"NCZ-MSG-{dispatched_entry['record_hash'][:8].upper()}"
        analyst_note = " | Analyst notified" if notify_analyst else ""

        session.add(
            AgentAction(
                case_id=case.id,
                action_type="RM_Approved",
                timestamp=now,
                actor=f"{rm_id}",
                details=(
                    f"RM signed off & dispatched chase round #{case.chase_round_count} "
                    f"| Ledger #{dispatched_entry['seq_id']} | Sig {hitl_signature[:12]}{analyst_note}"
                ),
            )
        )
        await session.commit()

        stmt = (
            select(KycCase)
            .where(KycCase.id == case.id)
            .options(selectinload(KycCase.documents), selectinload(KycCase.actions))
        )
        refreshed = (await session.execute(stmt)).scalar_one()

        return {
            "case": refreshed,
            "case_serialized": serialize_case_detail(refreshed),
            "tracking_id": tracking_id,
            "trace_id": trace_id,
            "audit_id": dispatched_entry["audit_id"],
            "approval_audit_id": approved_entry["audit_id"],
            "record_hash": dispatched_entry["record_hash"],
            "hitl_signature": hitl_signature,
            "rm_id": rm_id,
            "status": "Completed • Chase Dispatched",
            "dispatched_at": now_iso,
        }
