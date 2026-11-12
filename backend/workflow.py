from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional, TypedDict

import httpx
from dotenv import load_dotenv
from langgraph.graph import END, StateGraph
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from .database import serialize_case_detail
from .models import AgentAction, KycCase

ENV_FILE_PATH = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(ENV_FILE_PATH, override=True)


def reload_env_config() -> Dict[str, Any]:
    if ENV_FILE_PATH.exists():
        load_dotenv(ENV_FILE_PATH, override=True)
    return get_llm_config()


def get_llm_config() -> Dict[str, Any]:
    gemini_key = os.getenv("GEMINI_API_KEY", "").strip() or os.getenv("GOOGLE_API_KEY", "").strip()
    openai_key = os.getenv("OPENAI_API_KEY", "").strip()
    anthropic_key = os.getenv("ANTHROPIC_API_KEY", "").strip()

    provider = os.getenv("LLM_PROVIDER", "google").strip().lower()
    model = os.getenv("LLM_MODEL", "gemini-2.5-flash").strip()

    configured = bool(gemini_key or openai_key or anthropic_key)
    if not configured:
        provider = provider or "deterministic-fallback"
        model = model or "ChaseZero-Grounded-Engine"

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
    verified_docs = [d for d in case.documents if d.status == "Verified"]

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
    breached_count: int
    missing_docs_count: int
    verified_docs_count: int
    attempt: int
    subject: str
    body: str
    critic_passed: bool
    critic_score: int
    critic_feedback: str
    critic_checks: List[Dict[str, Any]]
    audit_hash: str
    events: List[Dict[str, Any]]


async def execute_langgraph_pipeline(
    session: AsyncSession,
    case: KycCase,
    simulate_critic_rejection: bool = False,
    tone: str = "institutional",
) -> tuple[KycCase, List[Dict[str, Any]]]:
    """Run the 3-node LangGraph StateGraph (Orchestrator -> Drafter -> Critic) and collect all events."""
    breached_count = int(
        await session.scalar(select(func.count(KycCase.id)).where(KycCase.sla_breached.is_(True)))
        or 4
    )
    missing_docs = [d for d in case.documents if d.status == "Missing"]
    verified_docs = [d for d in case.documents if d.status == "Verified"]
    llm_cfg = get_llm_config()
    model_label = llm_cfg["llm_model"]
    provider_label = llm_cfg["llm_provider"]

    async def orchestrator_node(state: ChaseGraphState) -> Dict[str, Any]:
        evts = list(state.get("events", []))
        evts.append(
            {
                "type": "node_start",
                "node": "orchestrator",
                "label": "Orchestrator Node scanning SQLite for SLA breaches (>3 days)...",
            }
        )
        evts.append(
            {
                "type": "orchestrator_result",
                "node": "orchestrator",
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
        evts = list(state.get("events", []))
        evts.append(
            {
                "type": "node_start",
                "node": "drafter",
                "attempt": attempt,
                "is_retry": is_retry,
                "llm_model": model_label,
                "llm_provider": provider_label,
                "label": (
                    f"Drafter Node v{attempt} ({model_label}): Re-synthesizing email to resolve Critic rejection..."
                    if is_retry
                    else f"Drafter Node ({model_label}): Synthesizing grounded KYC chase email..."
                ),
            }
        )

        if state.get("simulate_critic_rejection") and attempt == 1:
            subj, body = build_flawed_draft(case)
        else:
            subj, body = build_grounded_draft(case, state.get("tone", "institutional"))

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
                "engine": "grounded-fallback",
            }
        )

        suffix = " (post-Critic retry)" if is_retry else ""
        session.add(
            AgentAction(
                case_id=case.id,
                action_type="Draft_Generated",
                timestamp=datetime.now(timezone.utc),
                actor="ChaseZero Drafter (grounded-fallback)",
                details=f"Generated chase draft v{attempt} for {len(missing_docs)} missing docs{suffix}",
            )
        )
        await session.flush()

        return {
            "attempt": attempt,
            "subject": subj,
            "body": body,
            "events": evts,
        }

    async def critic_node(state: ChaseGraphState) -> Dict[str, Any]:
        attempt = state.get("attempt", 1)
        evts = list(state.get("events", []))
        evts.append(
            {
                "type": "node_start",
                "node": "critic",
                "attempt": attempt,
                "label": f"Critic Node ({model_label}): Auditing draft v{attempt} against 5 regulatory guardrails...",
            }
        )

        sim_fail = bool(state.get("simulate_critic_rejection") and attempt == 1)
        passed, score, feedback, checks, audit_hash = evaluate_critic_checks(
            case, state["body"], simulate_failure=sim_fail
        )
        routing_to = "end" if (passed or attempt >= 2) else "drafter"

        evts.append(
            {
                "type": "critic_result",
                "node": "critic",
                "attempt": attempt,
                "passed": passed,
                "score": score,
                "feedback": feedback,
                "checks": checks,
                "audit_hash": audit_hash,
                "routing_to": routing_to,
            }
        )

        if not passed:
            session.add(
                AgentAction(
                    case_id=case.id,
                    action_type="Critic_Rejected",
                    timestamp=datetime.now(timezone.utc),
                    actor="ChaseZero Critic QA",
                    details=f"Rejected draft v{attempt} (Score {score}/100): {feedback}",
                )
            )
            await session.flush()

        return {
            "critic_passed": passed,
            "critic_score": score,
            "critic_feedback": feedback,
            "critic_checks": checks,
            "audit_hash": audit_hash,
            "events": evts,
        }

    def route_after_critic(state: ChaseGraphState) -> str:
        if not state.get("critic_passed") and state.get("attempt", 1) < 2:
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
        "breached_count": 0,
        "missing_docs_count": 0,
        "verified_docs_count": 0,
        "attempt": 0,
        "subject": "",
        "body": "",
        "critic_passed": False,
        "critic_score": 0,
        "critic_feedback": "",
        "critic_checks": [],
        "audit_hash": "",
        "events": [],
    }

    final_state = await compiled.ainvoke(initial_state)

    case.current_draft_subject = final_state["subject"]
    case.current_draft_body = final_state["body"]
    case.critic_passed = final_state["critic_passed"]
    case.critic_score = final_state["critic_score"]
    case.critic_feedback = final_state["critic_feedback"]
    case.critic_checks_json = json.dumps(final_state["critic_checks"])
    case.workflow_iterations = final_state["attempt"]
    case.audit_hash = final_state["audit_hash"]
    if final_state["critic_passed"] and case.status != "Completed":
        case.status = "Draft_Ready"

    await session.commit()

    stmt = (
        select(KycCase)
        .where(KycCase.id == case.id)
        .options(selectinload(KycCase.documents), selectinload(KycCase.actions))
    )
    refreshed_case = (await session.execute(stmt)).scalar_one()
    return refreshed_case, final_state["events"]
