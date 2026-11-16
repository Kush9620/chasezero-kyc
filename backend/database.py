from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncGenerator, Dict, List, Optional

from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload

from .compliance_ledger import (
    ensure_compliance_tables,
    get_observability_summary,
    query_case_traces,
    seed_compliance_telemetry_if_empty,
)
from .models import AgentAction, Base, KycCase, KycDocument
from .schemas import AgentActionOut, CriticCheckItem, DocumentOut, KycCaseDetailOut, KycCaseOut

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./chasezero.db")

engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    future=True,
    connect_args={"timeout": 5.0},
)


@event.listens_for(engine.sync_engine, "connect")
def _set_sqlite_pragma(dbapi_connection: Any, connection_record: Any) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute("PRAGMA synchronous=NORMAL;")
    cursor.execute("PRAGMA foreign_keys=ON;")
    cursor.execute("PRAGMA busy_timeout=5000;")
    cursor.close()


AsyncSessionLocal = async_sessionmaker(
    bind=engine, class_=AsyncSession, expire_on_commit=False
)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        yield session


def serialize_action(action: AgentAction, case: KycCase | None = None) -> AgentActionOut:
    c = case or getattr(action, "kyc_case", None)
    ts = action.timestamp
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return AgentActionOut(
        id=action.id,
        case_id=action.case_id,
        case_code=c.case_code if c else None,
        client_name=c.client_name if c else None,
        action_type=action.action_type,
        timestamp=ts,
        actor=action.actor,
        details=action.details,
    )


def serialize_case(c: KycCase) -> KycCaseOut:
    try:
        raw_checks = json.loads(c.critic_checks_json or "[]")
    except Exception:
        raw_checks = []
    checks = [CriticCheckItem(**item) for item in raw_checks]
    docs = [
        DocumentOut(
            id=d.id,
            case_id=d.case_id,
            document_name=d.document_name,
            status=d.status,
            description=d.description,
            requirement_meta=d.requirement_meta,
            assigned_to_or_verifier=d.assigned_to_or_verifier,
            doc_code=d.doc_code,
        )
        for d in c.documents
    ]
    missing_count = sum(1 for d in docs if d.status == "Missing")
    verified_count = sum(1 for d in docs if d.status == "Verified")
    last_dt = c.last_contact_date
    if last_dt.tzinfo is None:
        last_dt = last_dt.replace(tzinfo=timezone.utc)
    trace_info = query_case_traces(c.case_code)
    total_cost = float(trace_info.get("total_cost_usd") or 0.0)
    latest_trace_id = (
        trace_info["traces"][0]["trace_id"] if trace_info.get("traces") else None
    )

    draft_subject = c.current_draft_subject
    draft_body = c.current_draft_body
    if not draft_body:
        first_name = (c.contact_name or "Client").split(" ")[0]
        missing_docs = [d for d in docs if d.status == "Missing"]
        doc_lines = "\n".join(
            f"{idx}. {d.document_name} — {d.description}"
            for idx, d in enumerate(missing_docs, start=1)
        ) or "1. Periodic Corporate KYC Confirmation"
        draft_subject = (
            draft_subject
            or f"Action Required: Finalizing KYC Documentation for {c.client_name} (Ref #{c.case_code})"
        )
        draft_body = (
            f"Dear {first_name},\n\n"
            f"Thank you for your partnership on {c.client_name}'s corporate institutional onboarding ({c.facility_value} facility). "
            f"To ensure we support your anticipated execution schedule ahead of {c.target_deadline_label} without delay, "
            f"we kindly request the following {len(missing_docs) or 1} outstanding document(s):\n\n"
            f"{doc_lines}\n\n"
            f"Please transmit these files strictly via your encrypted Northgate Secure Client Workspace link: "
            f"https://portal.northgatebank.com/secure-vault/{c.case_code.lower()}\n\n"
            f"Warm regards,\n"
            f"{c.assigned_rm}\n"
            f"VP, Corporate Banking Relationship Management\n"
            f"Northgate Commercial & Institutional Banking\n\n"
            f"CONFIDENTIALITY & COMPLIANCE NOTICE: This communication is issued pursuant to Northgate Risk & Settlement Protocols. "
            f"It does not constitute a legally binding loan commitment or final credit approval. "
            f"Northgate Bank plc is authorized and regulated by the PRA and FCA."
        )

    return KycCaseOut(
        id=c.id,
        case_code=c.case_code,
        client_name=c.client_name,
        status=c.status,
        days_open=c.days_open,
        last_contact_date=last_dt,
        days_since_contact=round(c.days_since_contact, 1),
        sla_breached=c.sla_breached,
        sla_delay_days=round(c.sla_delay_days, 1),
        chase_round_count=c.chase_round_count,
        lei=c.lei,
        entity_type=c.entity_type,
        jurisdiction=c.jurisdiction,
        desk=c.desk,
        region=c.region,
        address=c.address,
        contact_name=c.contact_name,
        contact_role=c.contact_role,
        contact_email=c.contact_email,
        assigned_rm=c.assigned_rm,
        facility_value=c.facility_value,
        relationship_notes=c.relationship_notes,
        target_deadline_label=c.target_deadline_label,
        current_draft_subject=draft_subject,
        current_draft_body=draft_body,
        critic_passed=c.critic_passed,
        critic_score=c.critic_score,
        critic_feedback=c.critic_feedback,
        critic_checks=checks,
        workflow_iterations=c.workflow_iterations,
        audit_hash=c.audit_hash,
        documents=docs,
        missing_count=missing_count,
        verified_count=verified_count,
        total_cost_usd=total_cost,
        latest_trace_id=latest_trace_id,
    )


def serialize_case_detail(c: KycCase) -> KycCaseDetailOut:
    base = serialize_case(c)
    actions = [serialize_action(a, c) for a in c.actions]
    return KycCaseDetailOut(**base.model_dump(), actions=actions)


AETHELGARD_CHECKS = [
    {
        "key": "tone",
        "title": "Tone & Register",
        "badge": "PASSED: Professional Executive",
        "passed": True,
        "summary": "No aggressive debt-collection phrasing, coercive language, or abrupt directives. Maintains respectful, institutional corporate banking cadence.",
        "meta_left": "Lexical register: Tier-1 Bank",
        "meta_right": "Sentiment: Neutral (+0.04)",
    },
    {
        "key": "accuracy",
        "title": "Document Accuracy",
        "badge": "PASSED: 100% Grounded",
        "passed": True,
        "summary": "Only documents flagged as 'Missing' in the tracker are cited. Zero hallucinated items; no already 'Verified' documents re-requested.",
        "meta_left": "Updated 2024 Register of Persons with Significant Control (PSC Register), Certified Proof of Operational Address for the Luxembourg Holding SPV",
        "meta_right": "2 missing items verified",
    },
    {
        "key": "promises",
        "title": "Promise & Liability Check",
        "badge": "PASSED: Zero Commitments",
        "passed": True,
        "summary": "Draft makes zero promises of unconditional credit approval, guaranteed timeline exemptions, or waiver of regulatory obligations.",
        "meta_left": "Liability Exposure: $0.00",
        "meta_right": "Warranty clauses: 0",
    },
    {
        "key": "pii",
        "title": "PII & Security Protocol",
        "badge": "PASSED: Compliant",
        "passed": True,
        "summary": "Contains authorized secure portal upload instructions; no unencrypted sensitive account numbers, tax IDs, or passwords exposed in cleartext.",
        "meta_left": "Encrypted Token: Valid",
        "meta_right": "GDPR/DPA 2018: Clear",
    },
    {
        "key": "disclosures",
        "title": "Regulatory Disclosures",
        "badge": "PASSED: Standard Northgate",
        "passed": True,
        "summary": "Standard Northgate Disclaimer attached. PRA/FCA regulatory notice, non-binding declaration, and confidentiality clause fully present.",
        "meta_left": "Footer Pattern: SEC-FCA-2024-R",
        "meta_right": "Signature Key: Valid",
    },
]

AETHELGARD_BODY = (
    "Dear Mr. Vance,\n\n"
    "We hope this message finds you well. As part of Northgate Capital's standard regulatory governance and in accordance with UK Financial Conduct Authority guidelines, we are currently concluding our scheduled annual KYC review for Aethelgard Global Holdings Ltd.\n\n"
    "To complete the audit file without delay, our compliance records require two outstanding entity documents:\n"
    "1. Updated 2024 Register of Persons with Significant Control (PSC Register)\n"
    "2. Certified Proof of Operational Address for the Luxembourg Holding SPV\n\n"
    "To ensure uninterrupted access to institutional credit facilities, we kindly request submission via our encrypted secure portal prior to November 1st.\n\n"
    "Please transmit these files strictly through the Northgate Secure Client Workspace link: https://portal.northgate.bank/upload/case-8841-auth-vance — Do not reply with unencrypted passport scans or identity records via email.\n\n"
    "Warm regards,\n"
    "Sarah Jenkins\n"
    "VP Corporate Banking, Northgate Capital Markets\n\n"
    "CONFIDENTIALITY & COMPLIANCE NOTICE: This communication is issued pursuant to Northgate Risk & Settlement Protocols. It does not constitute a legally binding loan commitment or final credit renewal authorization. Northgate Bank plc is authorized and regulated by the PRA and FCA."
)


async def init_db() -> int:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    ensure_compliance_tables()
    seed_compliance_telemetry_if_empty()

    async with AsyncSessionLocal() as session:
        count = await session.scalar(select(func.count(KycCase.id)))
        if not count or count == 0:
            await seed_db(session)
        count = await session.scalar(select(func.count(KycCase.id)))
        return int(count or 0)


async def seed_db(session: AsyncSession) -> None:
    now = datetime.now(timezone.utc)

    cases_data = [
        KycCase(
            id=1,
            case_code="NCZ-2024-8841",
            client_name="Apex Global Commodities Ltd",
            status="Missing_Docs",
            days_open=24,
            last_contact_date=now - timedelta(days=4, hours=5),
            days_since_contact=4.2,
            sla_breached=True,
            sla_delay_days=1.2,
            chase_round_count=2,
            lei="5493001KJTIIGC8Y1R12",
            entity_type="Multi-Jurisdictional Trading Firm",
            jurisdiction="UK Incorporated • Trading Entity",
            desk="Energy & Commodities",
            region="EMEA / UK",
            address="10 Fenchurch Avenue, London EC3M 5BN",
            contact_name="Alistair Finch",
            contact_role="General Counsel & Head of Onboarding",
            contact_email="a.finch@apexcommodities.co.uk",
            assigned_rm="Sarah Jenkins",
            facility_value="£45,000,000",
            relationship_notes="Alistair prefers communication before 11:00 AM London time. Mentioned upcoming board ratification this Friday; ensure he knows this protects their execution schedule.",
            target_deadline_label="November 1st",
            current_draft_subject=None,
            current_draft_body=None,
            critic_passed=False,
            critic_score=0,
            critic_feedback=None,
            critic_checks_json="[]",
            workflow_iterations=0,
            audit_hash=None,
        ),
        KycCase(
            id=2,
            case_code="NCZ-2024-8854",
            client_name="Meridian Capital Partners Fund IV",
            status="Missing_Docs",
            days_open=19,
            last_contact_date=now - timedelta(days=3, hours=19),
            days_since_contact=3.8,
            sla_breached=True,
            sla_delay_days=0.8,
            chase_round_count=1,
            lei="213800K98H45LMT2691",
            entity_type="Private Equity LP",
            jurisdiction="Cayman / UK Regulated Feeder",
            desk="FX & Rates Desk",
            region="EMEA / UK",
            address="25 Berkeley Square, Mayfair, London W1J 6HN",
            contact_name="Helena Vance",
            contact_role="Chief Compliance Officer",
            contact_email="h.vance@meridiancap.co.uk",
            assigned_rm="Sarah Jenkins",
            facility_value="£62,500,000",
            relationship_notes="Fund IV LP close scheduled next Wednesday. Board resolution requires dual director sign-off.",
            target_deadline_label="November 4th",
            current_draft_subject=None,
            current_draft_body=None,
            critic_passed=False,
            critic_score=0,
            critic_feedback=None,
            critic_checks_json="[]",
            workflow_iterations=0,
            audit_hash=None,
        ),
        KycCase(
            id=3,
            case_code="NCZ-2024-8862",
            client_name="Vanguard Logistics Nordics AS",
            status="Missing_Docs",
            days_open=12,
            last_contact_date=now - timedelta(days=3, hours=3),
            days_since_contact=3.1,
            sla_breached=True,
            sla_delay_days=0.1,
            chase_round_count=1,
            lei="5967007LIEEXZX890122",
            entity_type="Commercial Freight & Maritime",
            jurisdiction="Norway Incorporated • EEA Entity",
            desk="Structured Debt",
            region="EMEA / Nordics",
            address="Aker Brygge 14, 0250 Oslo, Norway",
            contact_name="Henrik Solberg",
            contact_role="VP Treasury & Risk",
            contact_email="h.solberg@vanguardnordics.no",
            assigned_rm="Sarah Jenkins",
            facility_value="£31,000,000",
            relationship_notes="Oslo registry apostille was delayed due to municipal holiday; follow up politely on W-8BEN-E.",
            target_deadline_label="November 6th",
            current_draft_subject=None,
            current_draft_body=None,
            critic_passed=False,
            critic_score=0,
            critic_feedback=None,
            critic_checks_json="[]",
            workflow_iterations=0,
            audit_hash=None,
        ),
        KycCase(
            id=4,
            case_code="NCZ-2024-8849",
            client_name="Aethelgard Global Holdings Ltd",
            status="Draft_Ready",
            days_open=21,
            last_contact_date=now - timedelta(days=3, hours=14),
            days_since_contact=3.6,
            sla_breached=True,
            sla_delay_days=0.6,
            chase_round_count=2,
            lei="549300V9N87YTR2P0431",
            entity_type="Institutional Holding Group",
            jurisdiction="UK / Luxembourg SPV Structure",
            desk="FX & Rates Desk",
            region="EMEA / UK",
            address="40 Bank Street, Canary Wharf, London E14 5NR",
            contact_name="Marcus Vance",
            contact_role="Group Treasurer",
            contact_email="m.vance@aethelgard-holdings.co.uk",
            assigned_rm="Sarah Jenkins",
            facility_value="£78,000,000",
            relationship_notes="Quarterly FX hedging renewal pending Luxembourg operational address verification.",
            target_deadline_label="November 1st",
            current_draft_subject="Northgate Commercial Banking: Annual Periodic KYC Review — Action Required [Ref: #NCZ-2024-8849]",
            current_draft_body=AETHELGARD_BODY,
            critic_passed=True,
            critic_score=100,
            critic_feedback="All 5 compliance and accuracy guardrails cleared.",
            critic_checks_json=json.dumps(AETHELGARD_CHECKS),
            workflow_iterations=1,
            audit_hash="#QA-9921-SEC-V3",
        ),
        KycCase(
            id=5,
            case_code="NCZ-2024-8870",
            client_name="Aura Renewable Tech Holdings",
            status="Missing_Docs",
            days_open=8,
            last_contact_date=now - timedelta(days=1, hours=12),
            days_since_contact=1.5,
            sla_breached=False,
            sla_delay_days=0.0,
            chase_round_count=1,
            lei="9845007D3F11A89E4C52",
            entity_type="Clean Energy Infrastructure",
            jurisdiction="UK / Germany Dual Listed",
            desk="Energy & Commodities",
            region="EMEA / EU",
            address="18 Finsbury Square, London EC2A 1AH",
            contact_name="Dr. Elena Rostova",
            contact_role="Chief Financial Officer",
            contact_email="e.rostova@aurarenewables.eu",
            assigned_rm="Sarah Jenkins",
            facility_value="£54,000,000",
            relationship_notes="Responsive team; last upload received 36h ago.",
            target_deadline_label="November 12th",
            current_draft_subject=None,
            current_draft_body=None,
            critic_passed=False,
            critic_score=0,
            critic_feedback=None,
            critic_checks_json="[]",
            workflow_iterations=0,
            audit_hash=None,
        ),
        KycCase(
            id=6,
            case_code="NCZ-2024-8875",
            client_name="Helios Shipping Syndicate Ltd",
            status="Missing_Docs",
            days_open=31,
            last_contact_date=now - timedelta(days=2, hours=3),
            days_since_contact=2.1,
            sla_breached=False,
            sla_delay_days=0.0,
            chase_round_count=2,
            lei="21380099KLMN11234098",
            entity_type="Maritime Syndicate",
            jurisdiction="Greece / UK Maritime Desk",
            desk="Structured Debt",
            region="EMEA / Mediterranean",
            address="88 Leadenhall Street, London EC3A 3BP",
            contact_name="Nikos Papadopoulos",
            contact_role="Syndicate Finance Director",
            contact_email="n.papadopoulos@heliosshipping.gr",
            assigned_rm="Sarah Jenkins",
            facility_value="£39,000,000",
            relationship_notes="21h remaining before 3-day SLA threshold.",
            target_deadline_label="November 14th",
            current_draft_subject=None,
            current_draft_body=None,
            critic_passed=False,
            critic_score=0,
            critic_feedback=None,
            critic_checks_json="[]",
            workflow_iterations=0,
            audit_hash=None,
        ),
        KycCase(
            id=7,
            case_code="NCZ-2024-8833",
            client_name="Sovereign Ridge Capital LLC",
            status="Missing_Docs",
            days_open=27,
            last_contact_date=now - timedelta(days=3, hours=10),
            days_since_contact=3.4,
            sla_breached=True,
            sla_delay_days=0.4,
            chase_round_count=2,
            lei="549300ABC987XYZ12345",
            entity_type="Multi-Strategy Credit Fund",
            jurisdiction="Delaware LLC / US SEC Registered",
            desk="FX & Rates Desk",
            region="Americas / US",
            address="350 Madison Avenue, New York, NY 10017",
            contact_name="Claire Sterling",
            contact_role="Managing Director, Operations",
            contact_email="c.sterling@sovereignridge.com",
            assigned_rm="Sarah Jenkins",
            facility_value="£85,000,000",
            relationship_notes="US FATCA W-9 certificate expired last month; needs refreshed execution.",
            target_deadline_label="November 2nd",
            current_draft_subject=None,
            current_draft_body=None,
            critic_passed=False,
            critic_score=0,
            critic_feedback=None,
            critic_checks_json="[]",
            workflow_iterations=0,
            audit_hash=None,
        ),
        KycCase(
            id=8,
            case_code="NCZ-2024-8802",
            client_name="Albion Trade Finance PLC",
            status="Completed",
            days_open=16,
            last_contact_date=now - timedelta(hours=7),
            days_since_contact=0.3,
            sla_breached=False,
            sla_delay_days=0.0,
            chase_round_count=2,
            lei="213800ALBION77654321",
            entity_type="Corporate Treasury Platform",
            jurisdiction="UK FCA Authorized",
            desk="FX & Rates Desk",
            region="EMEA / UK",
            address="1 Poultry, London EC2R 8EJ",
            contact_name="David Thorne",
            contact_role="Head of Compliance",
            contact_email="d.thorne@albiontrade.co.uk",
            assigned_rm="Sarah Jenkins",
            facility_value="£29,500,000",
            relationship_notes="All KYC docs received and verified after Round 2 ChaseZero dispatch.",
            target_deadline_label="October 29th",
            current_draft_subject=None,
            current_draft_body=None,
            critic_passed=True,
            critic_score=100,
            critic_feedback=None,
            critic_checks_json="[]",
            workflow_iterations=0,
            audit_hash="#CHASE-8802-DONE",
        ),
    ]

    docs_data = [
        KycDocument(
            id=1,
            case_id=1,
            document_name="Ultimate Beneficial Ownership (UBO) Registry Tier-2",
            status="Missing",
            description="Missing certified structure for holding entity in Luxembourg (Apex Lux S.à r.l. > 25% voting shares).",
            requirement_meta="Requirement: Cert. Copy < 90 days",
            assigned_to_or_verifier="Assigned: A. Finch (Apex GC)",
            doc_code="#UBO-LUX-8841",
        ),
        KycDocument(
            id=2,
            case_id=1,
            document_name="Certified Trust Deed (Addendum 2023)",
            status="Missing",
            description="Pending authorized corporate trustee counter-signature and legal opinion rider.",
            requirement_meta="Jurisdiction: Jersey / UK",
            assigned_to_or_verifier="No response to T+20 notice",
            doc_code="#TRD-JRS-2023",
        ),
        KycDocument(
            id=3,
            case_id=1,
            document_name="Certificate of Incorporation & Articles of Association",
            status="Verified",
            description="Companies House Stamp Valid",
            requirement_meta="Companies House Stamp Valid",
            assigned_to_or_verifier="Verified by Analyst Marcus Vance at 14:20 12-Oct",
            doc_code="#CH-UK-8841-A",
        ),
        KycDocument(
            id=4,
            case_id=1,
            document_name="Authorized Signatory List (W-8BEN-E Tax Form)",
            status="Received",
            description="Uploaded via client portal 2 days ago. Passed OCR triage, currently locked in human review.",
            requirement_meta="Analyst Review SLA: 18h remaining",
            assigned_to_or_verifier="Human-in-the-loop Active",
            doc_code="#TAX-W8-8841",
        ),
        KycDocument(
            id=5,
            case_id=1,
            document_name="AML Corporate Policy & Source of Funds Statement",
            status="Verified",
            description="Source: Barclays Institutional Acct",
            requirement_meta="Source: Barclays Institutional Acct",
            assigned_to_or_verifier="Verified by Analyst Sarah Lin at 09:15 10-Oct",
            doc_code="#SOF-GBP-7731",
        ),
        KycDocument(
            id=6,
            case_id=2,
            document_name="Authorized Signatory List (Resolution 4B)",
            status="Missing",
            description="Board resolution 4B requires dual general partner wet-ink or qualified e-signature.",
            requirement_meta="Requirement: Dual GP Execution",
            assigned_to_or_verifier="Assigned: H. Vance (Meridian CCO)",
            doc_code="#SIG-4B-8854",
        ),
        KycDocument(
            id=7,
            case_id=2,
            document_name="Certificate of Good Standing (Cayman CIMA)",
            status="Verified",
            description="Verified via CIMA Electronic Registry",
            requirement_meta="CIMA Registry Check Valid",
            assigned_to_or_verifier="Verified by Analyst Marcus Vance at 11:05 14-Oct",
            doc_code="#CIMA-8854-B",
        ),
        KycDocument(
            id=8,
            case_id=3,
            document_name="W-8BEN-E Form",
            status="Missing",
            description="Updated 2024 US withholding certification for USD freight clearing account.",
            requirement_meta="IRS Rev. Oct-2021 Format",
            assigned_to_or_verifier="Assigned: H. Solberg (VP Treasury)",
            doc_code="#W8B-NO-8862",
        ),
        KycDocument(
            id=9,
            case_id=3,
            document_name="Certificate of Incumbency",
            status="Missing",
            description="Apostilled Brønnøysund Register Centre officer incumbency extract.",
            requirement_meta="Jurisdiction: Norway EEA",
            assigned_to_or_verifier="Awaiting apostille upload",
            doc_code="#INC-NO-8862",
        ),
        KycDocument(
            id=10,
            case_id=3,
            document_name="Audited FY23 Consolidated Financial Statements",
            status="Verified",
            description="KPMG Oslo Audit Sign-off Confirmed",
            requirement_meta="Statutory Annual Filing",
            assigned_to_or_verifier="Verified by Analyst Sarah Lin at 16:40 13-Oct",
            doc_code="#FIN-NO-8862",
        ),
        KycDocument(
            id=11,
            case_id=4,
            document_name="Updated 2024 Register of Persons with Significant Control (PSC Register)",
            status="Missing",
            description="Updated shareholder register reflecting Q3 institutional equity rebalance.",
            requirement_meta="Companies House PSC01/02",
            assigned_to_or_verifier="Assigned: M. Vance (Group Treasurer)",
            doc_code="#PSC-UK-8849",
        ),
        KycDocument(
            id=12,
            case_id=4,
            document_name="Certified Proof of Operational Address for the Luxembourg Holding SPV",
            status="Missing",
            description="Recent utility or municipal domiciliation certificate (< 90 days).",
            requirement_meta="Jurisdiction: Luxembourg",
            assigned_to_or_verifier="Assigned: M. Vance (Group Treasurer)",
            doc_code="#ADDR-LUX-8849",
        ),
        KycDocument(
            id=13,
            case_id=4,
            document_name="Certificate of Incorporation & Memorandum",
            status="Verified",
            description="UK Companies House Certified Copy",
            requirement_meta="Verified < 30 days",
            assigned_to_or_verifier="Verified by Analyst Marcus Vance",
            doc_code="#COI-UK-8849",
        ),
        KycDocument(
            id=14,
            case_id=5,
            document_name="German Commercial Register Extract (Handelsregister B)",
            status="Missing",
            description="Current certified extract for Aura GmbH subsidiary.",
            requirement_meta="Jurisdiction: Germany",
            assigned_to_or_verifier="Due in 1.5 days",
            doc_code="#HRB-DE-8870",
        ),
        KycDocument(
            id=15,
            case_id=6,
            document_name="Vessel Beneficial Ownership Attestation",
            status="Missing",
            description="Signed attestation covering fleet SPVs under Marshall Islands flag.",
            requirement_meta="Maritime AML Standard",
            assigned_to_or_verifier="21h remaining in SLA",
            doc_code="#VBO-GR-8875",
        ),
        KycDocument(
            id=16,
            case_id=7,
            document_name="Refreshed IRS Form W-9 & FATCA GIIN Confirmation",
            status="Missing",
            description="Prior W-9 expired at Q3 close; updated signatory date required.",
            requirement_meta="Jurisdiction: US Delaware",
            assigned_to_or_verifier="Assigned: C. Sterling",
            doc_code="#W9-US-8833",
        ),
        KycDocument(
            id=17,
            case_id=7,
            document_name="SEC Form ADV Part 1A & 2A Filing",
            status="Verified",
            description="Matched against SEC IAPD registry",
            requirement_meta="SEC Registered Adviser",
            assigned_to_or_verifier="Verified by Analyst Sarah Lin",
            doc_code="#ADV-US-8833",
        ),
        KycDocument(
            id=18,
            case_id=8,
            document_name="FCA Firm Reference Number Authorization Letter",
            status="Verified",
            description="All onboarding clearances complete",
            requirement_meta="FCA Register Verified",
            assigned_to_or_verifier="Verified by Analyst Marcus Vance",
            doc_code="#FCA-UK-8802",
        ),
    ]

    actions_data: List[AgentAction] = []
    # Seed 19 Draft_Generated, 2 Critic_Rejected, 18 RM_Approved actions matching telemetry ledger
    draft_seeds = [
        (1, 13, 1),
        (5, 13, 1),
        (2, 12, 2),
        (6, 12, 2),
        (3, 11, 3),
        (7, 11, 3),
        (4, 10, 1),
        (8, 10, 1),
        (1, 9, 2),
        (5, 9, 2),
        (2, 8, 3),
        (6, 8, 3),
        (7, 7, 1),
        (3, 7, 1),
        (8, 6, 2),
        (1, 5, 3),
        (2, 4, 1),
        (3, 3, 2),
        (4, 2, 3),
    ]
    for idx, (cid, days_ago, rnd) in enumerate(draft_seeds, start=1):
        actions_data.append(
            AgentAction(
                id=idx,
                case_id=cid,
                action_type="Draft_Generated",
                timestamp=now - timedelta(days=days_ago, hours=idx % 6),
                actor="ChaseZero Drafter Agent",
                details=f"Synthesized grounded KYC chase draft (Round {rnd}) | 0 hallucinations",
            )
        )

    actions_data.append(
        AgentAction(
            id=20,
            case_id=1,
            action_type="Critic_Rejected",
            timestamp=now - timedelta(days=5, hours=3),
            actor="ChaseZero Critic QA",
            details="Intercepted speculative timeline guarantee on Draft v1; auto-routed back to Drafter Node for compliance correction.",
        )
    )
    actions_data.append(
        AgentAction(
            id=21,
            case_id=2,
            action_type="Critic_Rejected",
            timestamp=now - timedelta(days=4, hours=3),
            actor="ChaseZero Critic QA",
            details="Intercepted speculative timeline guarantee on Draft v1; auto-routed back to Drafter Node for compliance correction.",
        )
    )

    approved_cases = [1, 2, 3, 4, 5, 6, 7, 8, 1, 2, 3, 4, 5, 6, 7, 8, 1, 2]
    for offset, cid in enumerate(approved_cases):
        aid = 22 + offset
        actions_data.append(
            AgentAction(
                id=aid,
                case_id=cid,
                action_type="RM_Approved",
                timestamp=now - timedelta(days=max(2, 13 - (offset // 2)), hours=offset % 5),
                actor="Sarah Jenkins (RM)",
                details="RM signed off & dispatched via authenticated gateway (48s review time)",
            )
        )

    session.add_all(cases_data)
    session.add_all(docs_data)
    session.add_all(actions_data)
    await session.commit()
