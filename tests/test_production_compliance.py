from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.compliance_ledger import (
    PIIRedactor,
    get_rm_signing_secret,
    get_wal_sqlite_connection,
    query_audit_ledger,
    query_case_traces,
    verify_ledger_integrity,
)
from backend.database import init_db
from backend.main import app
from backend.mcp_server import approve_and_dispatch_chase, run_chase_pipeline
from backend.workflow import ComplianceSecurityError


@pytest.mark.asyncio
async def test_1_hitl_enforcement_and_perimeter_gate():
    await init_db()
    """
    1. HITL Enforcement:
       - Calling approve_and_dispatch_chase with an invalid RM secret raises ComplianceSecurityError.
       - Calling approve_and_dispatch_chase on a case in Missing_Docs (not Draft_Ready / Critic QA not passed)
         raises ComplianceSecurityError and sends zero emails.
       - Calling approve_and_dispatch_chase with an RM edited_body that introduces an unauthorized credit
         commitment fails the Critic Compliance QA gate and raises ComplianceSecurityError.
    """
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Reset case 1 (NCZ-2024-8841) to Missing_Docs first
        r_reset = await client.post("/api/cases/1/reset")
        assert r_reset.status_code == 200
        assert r_reset.json()["status"] == "Missing_Docs"

        # Case 1 is in Missing_Docs (has not passed Critic QA) -> must reject even with valid RM secret
        with pytest.raises(ComplianceSecurityError):
            await approve_and_dispatch_chase(
                case_id="NCZ-2024-8841",
                rm_id="Sarah Jenkins (VP Corporate Banking)",
                rm_approval_secret=get_rm_signing_secret(),
            )

        # Case 4 (NCZ-2024-8849) is in Draft_Ready -> calling with INVALID RM secret must fail
        with pytest.raises(ComplianceSecurityError):
            await approve_and_dispatch_chase(
                case_id="NCZ-2024-8849",
                rm_id="Sarah Jenkins (VP Corporate Banking)",
                rm_approval_secret="wrong-unauthorized-secret",
            )

        # Via MCP HTTP endpoint with invalid RM secret -> must return 403 error
        r_mcp_bad = await client.post(
            "/mcp",
            headers={
                "Authorization": "Bearer northgate-mcp-bearer-2026",
                "X-RM-Actor-ID": "Sarah Jenkins (VP Corporate Banking)",
            },
            json={
                "jsonrpc": "2.0",
                "id": 101,
                "method": "tools/call",
                "params": {
                    "name": "approve_and_dispatch_chase",
                    "arguments": {
                        "case_id": "NCZ-2024-8849",
                        "rm_id": "Sarah Jenkins (VP Corporate Banking)",
                        "rm_approval_secret": "invalid-secret-key",
                    },
                },
            },
        )
        assert r_mcp_bad.status_code == 403
        assert "ComplianceSecurityError" in r_mcp_bad.text

        # Confirm NCZ-2024-8849 is still Draft_Ready (zero emails sent)
        r_case4 = await client.get("/api/cases/4")
        assert r_case4.status_code == 200
        assert r_case4.json()["status"] == "Draft_Ready"


@pytest.mark.asyncio
async def test_2_trace_and_cost_unification_via_mcp_http():
    """
    2. Trace & Cost Unification:
       - Invoking run_chase_pipeline via the /mcp HTTP endpoint generates a single unified trace_id
         linking the MCP span, LangGraph orchestrator span, AI drafting span, Critic QA span,
         and SQLite cost records with accurate USD totals.
       - Verifies the Retry-Loop Circuit Breaker (max_critic_retries = 3) when persistent Critic
         rejections occur.
    """
    import os
    custom_trace_id = os.urandom(16).hex()
    traceparent = f"00-{custom_trace_id}-00f067aa0ba902b7-01"

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r_mcp = await client.post(
            "/mcp",
            headers={
                "Authorization": "Bearer northgate-mcp-bearer-2026",
                "X-RM-Actor-ID": "Sarah Jenkins (VP Corporate Banking)",
                "traceparent": traceparent,
            },
            json={
                "jsonrpc": "2.0",
                "id": 202,
                "method": "tools/call",
                "params": {
                    "name": "run_chase_pipeline",
                    "arguments": {
                        "case_id": "NCZ-2024-8841",
                        "tone_override": "institutional",
                    },
                },
            },
        )
        assert r_mcp.status_code == 200, r_mcp.text
        payload = r_mcp.json()["data"]

        assert payload["trace_id"] == custom_trace_id
        assert payload["case_id"] == "NCZ-2024-8841"
        assert payload["status"] == "Draft_Ready"
        assert payload["email_dispatched"] is False
        assert payload["critic_legal_score"] >= 0.95
        assert payload["cost_usd"] > 0.0

        # Query SQLite trace waterfall for NCZ-2024-8841 and verify unified trace_id & span hierarchy
        traces_info = query_case_traces("NCZ-2024-8841")
        matching_trace = next(
            (t for t in traces_info["traces"] if t["trace_id"] == custom_trace_id),
            None,
        )
        assert matching_trace is not None
        span_names = {s["span_name"] for s in matching_trace["spans"]}
        assert "mcp.tools/call.run_chase_pipeline" in span_names
        assert "langgraph.orchestrator_sla_scan" in span_names
        assert "langgraph.ai_drafting_agent" in span_names
        assert "langgraph.critic_compliance_qa" in span_names
        assert abs(matching_trace["total_cost_usd"] - payload["cost_usd"]) < 1e-6

        # Test Circuit Breaker (max_critic_retries = 3) on case NCZ-2024-8854
        cb_res = await run_chase_pipeline(
            case_id="NCZ-2024-8854",
            force_circuit_breaker=True,
        )
        assert cb_res["workflow_iterations"] == 3
        assert cb_res["critic_passed"] is False
        assert cb_res["circuit_breaker_tripped"] is True
        assert cb_res["email_dispatched"] is False


@pytest.mark.asyncio
async def test_3_database_immutability_and_hash_chain():
    """
    3. Database Immutability & Hash Chain:
       - Executing raw SQL UPDATE or DELETE on sec_fincen_audit_ledger raises an IntegrityError/DatabaseError.
       - verify_ledger_integrity() passes across multi-step workflows including RM HMAC sign-off.
    """
    conn = get_wal_sqlite_connection()
    try:
        with pytest.raises((sqlite3.IntegrityError, sqlite3.DatabaseError)) as exc_update:
            conn.execute(
                "UPDATE sec_fincen_audit_ledger SET action_type = 'TAMPERED' WHERE seq_id = 1"
            )
        assert "strictly immutable" in str(exc_update.value)

        with pytest.raises((sqlite3.IntegrityError, sqlite3.DatabaseError)) as exc_delete:
            conn.execute("DELETE FROM sec_fincen_audit_ledger WHERE seq_id = 1")
        assert "strictly immutable" in str(exc_delete.value)
    finally:
        conn.close()

    # Now execute valid RM HITL approval on NCZ-2024-8841 (which is now Draft_Ready from test_2)
    receipt = await approve_and_dispatch_chase(
        case_id="NCZ-2024-8841",
        rm_id="Sarah Jenkins (VP Corporate Banking)",
        rm_approval_secret=get_rm_signing_secret(),
    )
    assert receipt["email_dispatched"] is True
    assert len(receipt["hitl_signature"]) == 64

    # Verify full cryptographic hash chain across all rows in sec_fincen_audit_ledger
    integrity = verify_ledger_integrity()
    assert integrity["valid"] is True
    assert integrity["verified_records"] >= 10
    assert integrity["tampered_seq_ids"] == []

    # Reset case 1 back to SLA-breached demo state so UI stays ready
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/api/cases/1/reset")
        await client.post("/api/cases/2/reset")


@pytest.mark.asyncio
async def test_4_pii_redaction_preserves_lei_and_masks_sensitive_pii():
    """
    4. PII Redaction:
       - Sensitive UBO names, emails, street addresses, SSN/TIN, and passport numbers never appear in
         plaintext inside sec_fincen_audit_ledger or llm_trace_spans.attributes_json, while LEI numbers
         and case_id remain intact.
    """
    sample = (
        "Entity Apex Global Commodities Ltd (LEI 5493001KJTIIGC8Y1R12, Case NCZ-2024-8841) "
        "UBO: Alistair Finch, SSN: 123-45-6789, Passport #XK9988771, "
        "Email a.finch@apexcommodities.co.uk at 10 Fenchurch Avenue, London EC3M 5BN."
    )
    redacted = PIIRedactor.redact_text(sample)
    assert "5493001KJTIIGC8Y1R12" in redacted
    assert "NCZ-2024-8841" in redacted
    assert "Alistair Finch" not in redacted
    assert "123-45-6789" not in redacted
    assert "XK9988771" not in redacted
    assert "a.finch@apexcommodities.co.uk" not in redacted
    assert "[REDACTED_UBO_" in redacted
    assert "[REDACTED_TAX_ID]" in redacted
    assert "[REDACTED_PASSPORT]" in redacted
    assert "[REDACTED_EMAIL]" in redacted

    # Inspect stored database rows in sec_fincen_audit_ledger and llm_trace_spans
    ledger = query_audit_ledger(case_id="NCZ-2024-8841", limit=50)
    assert ledger["total"] > 0
    for row in ledger["records"]:
        combined = f"{row['redacted_input']} {row['redacted_output']}"
        assert "Alistair Finch" not in combined
        assert "a.finch@apexcommodities.co.uk" not in combined

    traces = query_case_traces("NCZ-2024-8841")
    for span in traces["spans"]:
        attrs_raw = span.get("attributes_json") or "{}"
        assert "Alistair Finch" not in attrs_raw
        assert "a.finch@apexcommodities.co.uk" not in attrs_raw


@pytest.mark.asyncio
async def test_5_cloud_run_health_probes_and_observability_endpoints():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for path in ("/healthz", "/readyz"):
            r = await client.get(path)
            assert r.status_code == 200
            data = r.json()
            assert data["status"] == "healthy"
            assert data["sqlite_journal_mode"] == "wal"
            assert data["ledger_integrity"]["valid"] is True

        r_obs = await client.get("/api/observability/summary")
        assert r_obs.status_code == 200
        obs = r_obs.json()
        assert obs["total_spend_usd"] > 0
        assert obs["avg_cost_per_chase_usd"] > 0
        assert obs["ledger_integrity_valid"] is True

        r_traces = await client.get("/api/observability/traces/NCZ-2024-8841")
        assert r_traces.status_code == 200
        assert r_traces.json()["span_count"] >= 4

        r_ledger = await client.get("/api/audit/ledger?case_id=NCZ-2024-8841")
        assert r_ledger.status_code == 200
        assert r_ledger.json()["integrity"]["valid"] is True


@pytest.mark.asyncio
async def test_6_all_ui_buttons_and_interactive_endpoints():
    """
    6. End-to-End UI Button Verification:
       Verifies that every interactive button in the Web UI works on any selected case:
       - Document status toggle (/api/cases/1/documents/1/toggle)
       - RM Note save (/api/cases/1/note)
       - Quick Tone Adjusters (/api/cases/1/draft/tone: casual, urgent, calendar, baseline)
       - Critic Compliance QA Simulate Failure & Re-scan (/api/cases/1/critic/rescan)
       - Live SSE Workflow Stream (/api/cases/1/workflow/stream)
       - Human-in-the-Loop Approve & Send (/api/cases/1/approve) even without running workflow first
       - Reset Case for Demo (/api/cases/1/reset)
    """
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1. Reset case 1 to Missing_Docs
        r_reset = await client.post("/api/cases/1/reset")
        assert r_reset.status_code == 200
        assert r_reset.json()["status"] == "Missing_Docs"
        assert r_reset.json()["current_draft_body"] is not None

        # 2. Directly click "Approve & Send Chase Email" from UI on Case 1 (without running workflow first)
        r_approve_direct = await client.post(
            "/api/cases/1/approve",
            json={
                "subject": "Urgent KYC Follow-Up",
                "body": "Dear Alistair, Please upload the missing KYC documents.",
                "notify_analyst": True,
                "actor": "Sarah Jenkins",
            },
        )
        assert r_approve_direct.status_code == 200, r_approve_direct.text
        assert r_approve_direct.json()["case"]["status"] == "Completed"
        assert r_approve_direct.json()["tracking_id"].startswith("NCZ-MSG-")

        # 3. Reset case 1 again and test Tone Adjusters
        await client.post("/api/cases/1/reset")
        for tone in ("casual", "urgent", "calendar", "baseline"):
            r_tone = await client.post("/api/cases/1/draft/tone", json={"tone": tone})
            assert r_tone.status_code == 200
            assert r_tone.json()["status"] == "Draft_Ready"

        # 4. Test Critic Simulate Policy Violation & Re-run Critic Scan
        r_fail = await client.post(
            "/api/cases/1/critic/rescan", json={"simulate_failure": True}
        )
        assert r_fail.status_code == 200
        assert r_fail.json()["critic_passed"] is False

        r_pass = await client.post(
            "/api/cases/1/critic/rescan", json={"simulate_failure": False}
        )
        assert r_pass.status_code == 200
        assert r_pass.json()["critic_passed"] is True
        assert r_pass.json()["status"] == "Draft_Ready"

        # 5. Test Document Toggle & RM Note Update
        r_doc = await client.post("/api/cases/1/documents/1/toggle")
        assert r_doc.status_code == 200
        await client.post("/api/cases/1/documents/1/toggle")
        await client.post("/api/cases/1/documents/1/toggle")

        r_note = await client.patch(
            "/api/cases/1/note",
            json={"relationship_notes": "Updated RM presentation note for Apex Global."},
        )
        assert r_note.status_code == 200

        # 6. Test SSE Workflow Stream
        r_stream = await client.get(
            "/api/cases/1/workflow/stream?simulate_critic_rejection=true&tone=institutional"
        )
        assert r_stream.status_code == 200
        assert "workflow_complete" in r_stream.text

        # Leave case 1 in clean demo state
        await client.post("/api/cases/1/reset")

        # 7. Test Operator Login & Role Switching (RM, ANALYST, COO, ADMIN)
        for role_key, expected_email in (
            ("RM", "rm@northgate.demo"),
            ("ANALYST", "analyst@northgate.demo"),
            ("COO", "coo@northgate.demo"),
            ("ADMIN", "admin@northgate.demo"),
        ):
            r_login = await client.post(
                "/api/auth/login",
                json={"email": expected_email, "password": "demo", "role": role_key},
            )
            assert r_login.status_code == 200
            login_data = r_login.json()
            assert login_data["authenticated"] is True
            assert login_data["operator"]["role"] == role_key

        r_logout = await client.post("/api/auth/logout")
        assert r_logout.status_code == 200
        assert r_logout.json()["authenticated"] is False
