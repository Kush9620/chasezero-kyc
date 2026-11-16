from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

DB_FILE_PATH = Path(__file__).resolve().parent.parent / "chasezero.db"

_LEDGER_LOCK = threading.RLock()


def get_db_path() -> str:
    db_url = os.getenv("DATABASE_URL", "")
    if db_url.startswith("sqlite") and ":///" in db_url:
        raw_path = db_url.split(":///", 1)[1]
        if raw_path and raw_path != ":memory:":
            return str(Path(raw_path).resolve())
    return str(DB_FILE_PATH)


def get_rm_signing_secret() -> str:
    return os.getenv("RM_SIGNING_SECRET", "northgate-rm-hmac-secret-2026").strip()


def get_mcp_auth_secret() -> str:
    return os.getenv("MCP_AUTH_SECRET", "northgate-mcp-bearer-2026").strip()


ACTIVE_SYSTEM_PROMPT = (
    "You are Northgate ChaseZero Autonomous Institutional KYC Drafter & Critic QA Engine. "
    "Enforce UK PRA/FCA, US SEC Rule 17a-4, and FinCEN CDD Rule (31 CFR 1010.230) compliance. "
    "Cite only missing documents grounded in SQLite; make zero credit commitments; attach encrypted portal URI."
)

ACTIVE_RULES_ENGINE = {
    "version": "2026.1-SEC-FINCEN",
    "min_critic_legal_score": 0.95,
    "max_critic_retries": 3,
    "require_rm_hmac_signature": True,
    "guardrails": ["tone", "accuracy", "promises", "pii", "disclosures"],
}


def compute_prompt_policy_hash(
    system_prompt: str = ACTIVE_SYSTEM_PROMPT,
    rules_engine: Optional[Dict[str, Any]] = None,
) -> str:
    rules_payload = json.dumps(rules_engine or ACTIVE_RULES_ENGINE, sort_keys=True)
    combined = f"{system_prompt}||{rules_payload}"
    return hashlib.sha256(combined.encode("utf-8")).hexdigest()


def get_wal_sqlite_connection(db_path: Optional[str] = None) -> sqlite3.Connection:
    """Return a production-configured SQLite connection with WAL mode and 5000ms busy_timeout."""
    target = db_path or get_db_path()
    conn = sqlite3.connect(target, timeout=5.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn


class PIIRedactor:
    """
    Deterministic PII Redactor that preserves institutional identifiers (LEI, case_id,
    corporate entity name, jurisdiction) while deterministically tokenizing sensitive personal
    identifiers (UBO personal names, SSN/TIN/EIN, passport numbers, personal emails, street addresses).
    """

    KNOWN_PERSONAL_NAMES = [
        "Alistair Finch",
        "Helena Vance",
        "Henrik Solberg",
        "Marcus Vance",
        "Dr. Elena Rostova",
        "Elena Rostova",
        "Nikos Papadopoulos",
        "Claire Sterling",
        "David Thorne",
        "John Doe",
        "Jane Smith",
        "Alexander Petrov",
        "carlos.mendez",
        "Carlos Mendez",
    ]

    LEI_PATTERN = re.compile(r"\b[0-9A-Z]{18}[0-9]{2}\b")
    CASE_ID_PATTERN = re.compile(r"\bNCZ-\d{4}-\d{4}\b")

    SSN_TIN_PATTERN = re.compile(
        r"(?:\b\d{3}-\d{2}-\d{4}\b|\b\d{2}-\d{7}\b|(?:SSN|TIN|EIN|Tax\s*ID)\s*[:#=-]?\s*[A-Z0-9-]{6,15})",
        re.IGNORECASE,
    )
    PASSPORT_PATTERN = re.compile(
        r"(?:Passport(?:\s+No\.?|\s+#|\s+Number)?\s*[:#=-]?\s*[A-Z0-9]{6,12})",
        re.IGNORECASE,
    )
    EMAIL_PATTERN = re.compile(
        r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
    )
    ADDRESS_PATTERN = re.compile(
        r"\b\d{1,5}\s+[A-Z][A-Za-z0-9\s,.]{2,35}\b(?:Avenue|Ave|Street|St|Square|Sq|Road|Rd|Boulevard|Blvd|Lane|Ln|Brygge|Poultry|Wharf)\b(?:\s*,?\s*[A-Za-z0-9\s]{2,20})?",
        re.IGNORECASE,
    )
    UBO_INLINE_PATTERN = re.compile(
        r"(?:UBO|Beneficial\s+Owner|Natural\s+Person|Director)\s*[:=-]\s*([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)"
    )

    @classmethod
    def _ubo_token(cls, name: str) -> str:
        digest = hashlib.sha256(name.strip().lower().encode("utf-8")).hexdigest()[:6].upper()
        return f"[REDACTED_UBO_{digest}]"

    @classmethod
    def redact_text(cls, text: Optional[str]) -> str:
        if not text:
            return ""
        out = str(text)

        # Protect LEIs and Case IDs with placeholders so they are never accidentally altered
        protected: Dict[str, str] = {}

        def _protect(match: re.Match) -> str:
            val = match.group(0)
            key = f"__PROTECTED_{len(protected)}__"
            protected[key] = val
            return key

        out = cls.LEI_PATTERN.sub(_protect, out)
        out = cls.CASE_ID_PATTERN.sub(_protect, out)

        # Redact explicit UBO inline mentions
        def _replace_ubo_inline(match: re.Match) -> str:
            full = match.group(0)
            person = match.group(1)
            return full.replace(person, cls._ubo_token(person))

        out = cls.UBO_INLINE_PATTERN.sub(_replace_ubo_inline, out)

        # Redact known personal contact/UBO names deterministically
        for name in sorted(cls.KNOWN_PERSONAL_NAMES, key=len, reverse=True):
            pattern = re.compile(re.escape(name), re.IGNORECASE)
            out = pattern.sub(cls._ubo_token(name), out)

        # Also redact Firstname Lastname after Dear / Hi salutation
        out = re.sub(
            r"\b(Dear|Hi)\s+(?:Mr\.|Ms\.|Mrs\.|Dr\.)?\s*([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)",
            lambda m: f"{m.group(1)} {cls._ubo_token(m.group(2))}",
            out,
        )

        # Redact Tax IDs (SSN / TIN / EIN)
        out = cls.SSN_TIN_PATTERN.sub("[REDACTED_TAX_ID]", out)

        # Redact Passport numbers
        out = cls.PASSPORT_PATTERN.sub("[REDACTED_PASSPORT]", out)

        # Redact Email addresses
        out = cls.EMAIL_PATTERN.sub("[REDACTED_EMAIL]", out)

        # Redact physical street addresses
        out = cls.ADDRESS_PATTERN.sub("[REDACTED_ADDRESS]", out)

        # Restore protected institutional identifiers (LEI & Case ID)
        for key, val in protected.items():
            out = out.replace(key, val)

        return out

    @classmethod
    def redact_obj(cls, obj: Any) -> Any:
        if isinstance(obj, str):
            return cls.redact_text(obj)
        if isinstance(obj, dict):
            return {k: cls.redact_obj(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [cls.redact_obj(item) for item in obj]
        return obj


class ModelCostEngine:
    """
    Granular Per-Call, Per-Node, and Per-Case USD Cost Engine with live model price card
    (USD per 1M input, cached input, and output tokens).
    """

    PRICE_CARD_PER_1M: Dict[str, Dict[str, float]] = {
        # Google Gemini / Vertex AI
        "gemini-2.5-flash": {"input": 0.30, "cached": 0.075, "output": 2.50, "provider": "google_vertex"},
        "gemini-2.5-pro": {"input": 1.25, "cached": 0.3125, "output": 10.00, "provider": "google_vertex"},
        "gemini-2.0-flash": {"input": 0.15, "cached": 0.0375, "output": 0.60, "provider": "google_vertex"},
        # OpenAI
        "gpt-4o": {"input": 2.50, "cached": 1.25, "output": 10.00, "provider": "openai"},
        "gpt-4o-mini": {"input": 0.15, "cached": 0.075, "output": 0.60, "provider": "openai"},
        "o3-mini": {"input": 1.10, "cached": 0.55, "output": 4.40, "provider": "openai"},
        # Anthropic
        "claude-3-7-sonnet": {"input": 3.00, "cached": 0.30, "output": 15.00, "provider": "anthropic"},
        "claude-3-5-sonnet": {"input": 3.00, "cached": 0.30, "output": 15.00, "provider": "anthropic"},
        "claude-3-5-haiku": {"input": 0.80, "cached": 0.08, "output": 4.00, "provider": "anthropic"},
        # Grounded Enterprise Engine (attributed against Gemini 2.5 Pro/Flash institutional SLA tier)
        "chasezero-grounded-engine": {"input": 1.25, "cached": 0.3125, "output": 10.00, "provider": "google_vertex"},
    }

    @classmethod
    def resolve_pricing(cls, model_name: Optional[str]) -> Dict[str, Any]:
        key = (model_name or "gemini-2.5-flash").strip().lower()
        if key in cls.PRICE_CARD_PER_1M:
            return cls.PRICE_CARD_PER_1M[key]
        for registered, rates in cls.PRICE_CARD_PER_1M.items():
            if registered in key or key in registered:
                return rates
        return cls.PRICE_CARD_PER_1M["gemini-2.5-flash"]

    @classmethod
    def compute_cost_usd(
        cls,
        model_name: Optional[str],
        input_tokens: int,
        output_tokens: int,
        cached_tokens: int = 0,
    ) -> float:
        rates = cls.resolve_pricing(model_name)
        in_tokens = max(0, int(input_tokens or 0))
        cache_tok = max(0, min(in_tokens, int(cached_tokens or 0)))
        uncached_in = max(0, in_tokens - cache_tok)
        out_tokens = max(0, int(output_tokens or 0))

        usd = (
            (uncached_in * rates["input"])
            + (cache_tok * rates["cached"])
            + (out_tokens * rates["output"])
        ) / 1_000_000.0
        return round(usd, 6)

    @classmethod
    def estimate_tokens(cls, text: Optional[str]) -> int:
        if not text:
            return 0
        # Institutional token estimation (~3.6 chars per token + prompt framing overhead)
        return max(12, int(round(len(text) / 3.6)))


def compute_ledger_record_hash(
    seq_id: int,
    audit_id: str,
    prev_record_hash: str,
    trace_id: str,
    case_id: str,
    actor_id: str,
    action_type: str,
    prompt_policy_hash: str,
    redacted_input: str,
    redacted_output: str,
    hitl_signature: Optional[str],
    created_at: str,
) -> str:
    redacted_payload_hash = hashlib.sha256(
        f"{redacted_input}||{redacted_output}".encode("utf-8")
    ).hexdigest()
    sig_part = hitl_signature or "UNSIGNED"
    canonical = (
        f"{seq_id}|{audit_id}|{prev_record_hash}|{trace_id}|{case_id}|"
        f"{actor_id}|{action_type}|{prompt_policy_hash}|"
        f"{redacted_payload_hash}|{sig_part}|{created_at}"
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compute_hitl_hmac_signature(
    case_id: str,
    rm_id: str,
    final_body: str,
    timestamp_iso: str,
    signing_secret: Optional[str] = None,
) -> str:
    secret = (signing_secret or get_rm_signing_secret()).encode("utf-8")
    final_body_hash = hashlib.sha256((final_body or "").encode("utf-8")).hexdigest()
    message = f"{case_id}:{rm_id}:{final_body_hash}:{timestamp_iso}".encode("utf-8")
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def verify_rm_approval_secret(provided_secret: Optional[str]) -> bool:
    if not provided_secret:
        return False
    expected = get_rm_signing_secret()
    return hmac.compare_digest(provided_secret.strip(), expected)


def ensure_compliance_tables(db_path: Optional[str] = None) -> None:
    """Create llm_trace_spans, sec_fincen_audit_ledger, indexes, and immutability triggers."""
    with _LEDGER_LOCK:
        conn = get_wal_sqlite_connection(db_path)
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS llm_trace_spans (
                    span_id TEXT PRIMARY KEY,
                    trace_id TEXT NOT NULL,
                    parent_span_id TEXT,
                    case_id TEXT,
                    round_number INTEGER DEFAULT 1,
                    span_name TEXT NOT NULL,
                    operation_name TEXT NOT NULL,
                    provider_name TEXT,
                    model_name TEXT,
                    input_tokens INTEGER DEFAULT 0,
                    output_tokens INTEGER DEFAULT 0,
                    cached_tokens INTEGER DEFAULT 0,
                    cost_usd REAL DEFAULT 0.0,
                    latency_ms REAL NOT NULL,
                    status TEXT NOT NULL,
                    error_message TEXT,
                    attributes_json TEXT,
                    started_at TEXT NOT NULL,
                    ended_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_llm_trace_spans_case_id
                    ON llm_trace_spans(case_id);
                CREATE INDEX IF NOT EXISTS idx_llm_trace_spans_trace_id
                    ON llm_trace_spans(trace_id);

                CREATE TABLE IF NOT EXISTS sec_fincen_audit_ledger (
                    seq_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    audit_id TEXT UNIQUE NOT NULL,
                    prev_record_hash TEXT NOT NULL,
                    record_hash TEXT UNIQUE NOT NULL,
                    trace_id TEXT NOT NULL,
                    case_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    actor_role TEXT NOT NULL,
                    entry_channel TEXT NOT NULL,
                    action_type TEXT NOT NULL,
                    prompt_policy_hash TEXT NOT NULL,
                    redacted_input TEXT NOT NULL,
                    redacted_output TEXT NOT NULL,
                    critic_legal_score REAL,
                    critic_politeness_score REAL,
                    critic_findings_json TEXT,
                    hitl_rm_id TEXT,
                    hitl_signature TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_sec_fincen_audit_case_id
                    ON sec_fincen_audit_ledger(case_id);
                CREATE INDEX IF NOT EXISTS idx_sec_fincen_audit_trace_id
                    ON sec_fincen_audit_ledger(trace_id);

                CREATE TRIGGER IF NOT EXISTS prevent_audit_update
                BEFORE UPDATE ON sec_fincen_audit_ledger
                BEGIN
                    SELECT RAISE(ABORT, 'SEC/FinCEN Compliance Violation: Audit ledger is strictly immutable.');
                END;

                CREATE TRIGGER IF NOT EXISTS prevent_audit_delete
                BEFORE DELETE ON sec_fincen_audit_ledger
                BEGIN
                    SELECT RAISE(ABORT, 'SEC/FinCEN Compliance Violation: Audit ledger is strictly immutable.');
                END;
                """
            )
            conn.commit()
        finally:
            conn.close()


def append_audit_ledger_entry(
    *,
    trace_id: str,
    case_id: str,
    actor_id: str,
    actor_role: str,
    entry_channel: str,
    action_type: str,
    raw_input: str,
    raw_output: str,
    critic_legal_score: Optional[float] = None,
    critic_politeness_score: Optional[float] = None,
    critic_findings: Optional[Any] = None,
    hitl_rm_id: Optional[str] = None,
    hitl_signature: Optional[str] = None,
    created_at: Optional[str] = None,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Deterministically redact PII from raw_input & raw_output, compute cryptographic
    SHA-256 hash chain link from the previous row, and insert an immutable ledger entry.
    """
    ensure_compliance_tables(db_path)
    redacted_in = PIIRedactor.redact_text(raw_input)
    redacted_out = PIIRedactor.redact_text(raw_output)
    findings_redacted = (
        json.dumps(PIIRedactor.redact_obj(critic_findings))
        if critic_findings is not None
        else None
    )
    policy_hash = compute_prompt_policy_hash()
    ts = created_at or datetime.now(timezone.utc).isoformat()
    audit_id = str(uuid.uuid4())

    with _LEDGER_LOCK:
        conn = get_wal_sqlite_connection(db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT seq_id, record_hash FROM sec_fincen_audit_ledger ORDER BY seq_id DESC LIMIT 1"
            ).fetchone()
            if row is None:
                next_seq_id = 1
                prev_hash = "GENESIS"
            else:
                next_seq_id = int(row["seq_id"]) + 1
                prev_hash = str(row["record_hash"])

            record_hash = compute_ledger_record_hash(
                seq_id=next_seq_id,
                audit_id=audit_id,
                prev_record_hash=prev_hash,
                trace_id=trace_id,
                case_id=case_id,
                actor_id=actor_id,
                action_type=action_type,
                prompt_policy_hash=policy_hash,
                redacted_input=redacted_in,
                redacted_output=redacted_out,
                hitl_signature=hitl_signature,
                created_at=ts,
            )

            conn.execute(
                """
                INSERT INTO sec_fincen_audit_ledger (
                    seq_id, audit_id, prev_record_hash, record_hash,
                    trace_id, case_id, actor_id, actor_role, entry_channel,
                    action_type, prompt_policy_hash, redacted_input, redacted_output,
                    critic_legal_score, critic_politeness_score, critic_findings_json,
                    hitl_rm_id, hitl_signature, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    next_seq_id,
                    audit_id,
                    prev_hash,
                    record_hash,
                    trace_id,
                    case_id,
                    actor_id,
                    actor_role,
                    entry_channel,
                    action_type,
                    policy_hash,
                    redacted_in,
                    redacted_out,
                    critic_legal_score,
                    critic_politeness_score,
                    findings_redacted,
                    hitl_rm_id,
                    hitl_signature,
                    ts,
                ),
            )
            conn.commit()
            return {
                "seq_id": next_seq_id,
                "audit_id": audit_id,
                "prev_record_hash": prev_hash,
                "record_hash": record_hash,
                "trace_id": trace_id,
                "case_id": case_id,
                "actor_id": actor_id,
                "actor_role": actor_role,
                "entry_channel": entry_channel,
                "action_type": action_type,
                "prompt_policy_hash": policy_hash,
                "redacted_input": redacted_in,
                "redacted_output": redacted_out,
                "critic_legal_score": critic_legal_score,
                "critic_politeness_score": critic_politeness_score,
                "hitl_rm_id": hitl_rm_id,
                "hitl_signature": hitl_signature,
                "created_at": ts,
            }
        finally:
            conn.close()


def insert_trace_span(
    *,
    span_id: str,
    trace_id: str,
    parent_span_id: Optional[str],
    case_id: Optional[str],
    round_number: int = 1,
    span_name: str,
    operation_name: str,
    provider_name: Optional[str] = "internal",
    model_name: Optional[str] = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cached_tokens: int = 0,
    cost_usd: float = 0.0,
    latency_ms: float = 0.0,
    status: str = "OK",
    error_message: Optional[str] = None,
    attributes: Optional[Dict[str, Any]] = None,
    started_at: Optional[str] = None,
    ended_at: Optional[str] = None,
    db_path: Optional[str] = None,
) -> None:
    ensure_compliance_tables(db_path)
    sanitized_attrs = PIIRedactor.redact_obj(attributes or {})
    attrs_json = json.dumps(sanitized_attrs, default=str)
    now_iso = datetime.now(timezone.utc).isoformat()
    start_iso = started_at or now_iso
    end_iso = ended_at or now_iso

    with _LEDGER_LOCK:
        conn = get_wal_sqlite_connection(db_path)
        try:
            conn.execute(
                """
                INSERT OR REPLACE INTO llm_trace_spans (
                    span_id, trace_id, parent_span_id, case_id, round_number,
                    span_name, operation_name, provider_name, model_name,
                    input_tokens, output_tokens, cached_tokens, cost_usd,
                    latency_ms, status, error_message, attributes_json,
                    started_at, ended_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    span_id,
                    trace_id,
                    parent_span_id,
                    case_id,
                    int(round_number or 1),
                    span_name,
                    operation_name,
                    provider_name,
                    model_name,
                    int(input_tokens or 0),
                    int(output_tokens or 0),
                    int(cached_tokens or 0),
                    round(float(cost_usd or 0.0), 6),
                    round(float(latency_ms or 0.0), 2),
                    status,
                    PIIRedactor.redact_text(error_message) if error_message else None,
                    attrs_json,
                    start_iso,
                    end_iso,
                ),
            )
            conn.commit()
        finally:
            conn.close()


def verify_ledger_integrity(db_path: Optional[str] = None) -> Dict[str, Any]:
    """
    Walks sec_fincen_audit_ledger sequentially, recomputes every record_hash and
    verifies prev_record_hash linkage across the entire chain.
    """
    ensure_compliance_tables(db_path)
    with _LEDGER_LOCK:
        conn = get_wal_sqlite_connection(db_path)
        try:
            rows = conn.execute(
                "SELECT * FROM sec_fincen_audit_ledger ORDER BY seq_id ASC"
            ).fetchall()
        finally:
            conn.close()

    tampered_seq_ids: List[int] = []
    expected_prev = "GENESIS"
    head_hash = "GENESIS"

    for r in rows:
        seq_id = int(r["seq_id"])
        prev_hash = str(r["prev_record_hash"])
        stored_hash = str(r["record_hash"])

        recomputed = compute_ledger_record_hash(
            seq_id=seq_id,
            audit_id=str(r["audit_id"]),
            prev_record_hash=prev_hash,
            trace_id=str(r["trace_id"]),
            case_id=str(r["case_id"]),
            actor_id=str(r["actor_id"]),
            action_type=str(r["action_type"]),
            prompt_policy_hash=str(r["prompt_policy_hash"]),
            redacted_input=str(r["redacted_input"]),
            redacted_output=str(r["redacted_output"]),
            hitl_signature=r["hitl_signature"],
            created_at=str(r["created_at"]),
        )

        if prev_hash != expected_prev or stored_hash != recomputed:
            tampered_seq_ids.append(seq_id)

        expected_prev = stored_hash
        head_hash = stored_hash

    return {
        "valid": len(tampered_seq_ids) == 0,
        "verified_records": len(rows),
        "tampered_seq_ids": tampered_seq_ids,
        "head_hash": head_hash,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


def query_audit_ledger(
    case_id: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    ensure_compliance_tables(db_path)
    integrity = verify_ledger_integrity(db_path)
    with _LEDGER_LOCK:
        conn = get_wal_sqlite_connection(db_path)
        try:
            if case_id:
                rows = conn.execute(
                    """
                    SELECT * FROM sec_fincen_audit_ledger
                    WHERE case_id = ?
                    ORDER BY seq_id DESC
                    LIMIT ? OFFSET ?
                    """,
                    (case_id, limit, offset),
                ).fetchall()
                total = conn.execute(
                    "SELECT COUNT(*) FROM sec_fincen_audit_ledger WHERE case_id = ?",
                    (case_id,),
                ).fetchone()[0]
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM sec_fincen_audit_ledger
                    ORDER BY seq_id DESC
                    LIMIT ? OFFSET ?
                    """,
                    (limit, offset),
                ).fetchall()
                total = conn.execute(
                    "SELECT COUNT(*) FROM sec_fincen_audit_ledger"
                ).fetchone()[0]
        finally:
            conn.close()

    records = []
    for r in rows:
        item = dict(r)
        try:
            item["critic_findings"] = (
                json.loads(item["critic_findings_json"])
                if item.get("critic_findings_json")
                else []
            )
        except Exception:
            item["critic_findings"] = []
        records.append(item)

    return {
        "integrity": integrity,
        "total": int(total),
        "limit": limit,
        "offset": offset,
        "records": records,
    }


def query_case_traces(
    case_id: str,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    ensure_compliance_tables(db_path)
    with _LEDGER_LOCK:
        conn = get_wal_sqlite_connection(db_path)
        try:
            rows = conn.execute(
                """
                SELECT * FROM llm_trace_spans
                WHERE case_id = ?
                ORDER BY started_at ASC, rowid ASC
                """,
                (case_id,),
            ).fetchall()
        finally:
            conn.close()

    spans: List[Dict[str, Any]] = []
    traces_map: Dict[str, Dict[str, Any]] = {}
    total_cost_usd = 0.0
    total_input_tokens = 0
    total_output_tokens = 0
    total_cached_tokens = 0

    for r in rows:
        s = dict(r)
        try:
            s["attributes"] = json.loads(s.get("attributes_json") or "{}")
        except Exception:
            s["attributes"] = {}
        spans.append(s)

        tid = s["trace_id"]
        cost = float(s.get("cost_usd") or 0.0)
        in_tok = int(s.get("input_tokens") or 0)
        out_tok = int(s.get("output_tokens") or 0)
        cache_tok = int(s.get("cached_tokens") or 0)

        total_cost_usd += cost
        total_input_tokens += in_tok
        total_output_tokens += out_tok
        total_cached_tokens += cache_tok

        if tid not in traces_map:
            traces_map[tid] = {
                "trace_id": tid,
                "case_id": case_id,
                "round_number": s.get("round_number", 1),
                "started_at": s["started_at"],
                "ended_at": s["ended_at"],
                "total_cost_usd": 0.0,
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "total_cached_tokens": 0,
                "total_latency_ms": 0.0,
                "status": "OK",
                "spans": [],
            }
        t_entry = traces_map[tid]
        t_entry["ended_at"] = max(t_entry["ended_at"], s["ended_at"])
        t_entry["total_cost_usd"] = round(t_entry["total_cost_usd"] + cost, 6)
        t_entry["total_input_tokens"] += in_tok
        t_entry["total_output_tokens"] += out_tok
        t_entry["total_cached_tokens"] += cache_tok
        if s.get("parent_span_id") is None:
            t_entry["total_latency_ms"] = round(float(s.get("latency_ms") or 0.0), 2)
        elif t_entry["total_latency_ms"] == 0.0:
            t_entry["total_latency_ms"] = round(
                t_entry["total_latency_ms"] + float(s.get("latency_ms") or 0.0), 2
            )
        if s.get("status") in ("ERROR", "CRITIC_REJECTED"):
            t_entry["status"] = s["status"]
        t_entry["spans"].append(s)

    traces_list = list(traces_map.values())
    traces_list.sort(key=lambda x: x["started_at"], reverse=True)

    return {
        "case_id": case_id,
        "trace_count": len(traces_list),
        "span_count": len(spans),
        "total_cost_usd": round(total_cost_usd, 6),
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
        "total_cached_tokens": total_cached_tokens,
        "traces": traces_list,
        "spans": spans,
    }


def get_observability_summary(db_path: Optional[str] = None) -> Dict[str, Any]:
    ensure_compliance_tables(db_path)
    integrity = verify_ledger_integrity(db_path)
    with _LEDGER_LOCK:
        conn = get_wal_sqlite_connection(db_path)
        try:
            agg = conn.execute(
                """
                SELECT
                    COUNT(DISTINCT trace_id) AS trace_count,
                    COUNT(*) AS span_count,
                    COALESCE(SUM(cost_usd), 0.0) AS total_spend_usd,
                    COALESCE(SUM(input_tokens), 0) AS total_input_tokens,
                    COALESCE(SUM(output_tokens), 0) AS total_output_tokens,
                    COALESCE(SUM(cached_tokens), 0) AS total_cached_tokens,
                    COALESCE(AVG(CASE WHEN parent_span_id IS NULL THEN latency_ms END), 412.5) AS avg_trace_latency_ms
                FROM llm_trace_spans
                """
            ).fetchone()

            per_case_rows = conn.execute(
                """
                SELECT
                    case_id,
                    COALESCE(SUM(cost_usd), 0.0) AS cost_usd,
                    COALESCE(SUM(input_tokens + output_tokens), 0) AS total_tokens,
                    COUNT(DISTINCT trace_id) AS traces
                FROM llm_trace_spans
                WHERE case_id IS NOT NULL
                GROUP BY case_id
                """
            ).fetchall()
        finally:
            conn.close()

    trace_count = max(1, int(agg["trace_count"] or 0))
    total_spend_usd = round(float(agg["total_spend_usd"] or 0.0), 6)
    avg_cost_per_chase_usd = round(total_spend_usd / trace_count, 6)
    total_input = int(agg["total_input_tokens"] or 0)
    total_output = int(agg["total_output_tokens"] or 0)
    total_cached = int(agg["total_cached_tokens"] or 0)

    case_costs = {
        str(r["case_id"]): {
            "cost_usd": round(float(r["cost_usd"] or 0.0), 6),
            "total_tokens": int(r["total_tokens"] or 0),
            "traces": int(r["traces"] or 0),
        }
        for r in per_case_rows
    }

    return {
        "total_spend_usd": total_spend_usd,
        "avg_cost_per_chase_usd": avg_cost_per_chase_usd,
        "total_tokens": total_input + total_output,
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "total_cached_tokens": total_cached,
        "trace_count": int(agg["trace_count"] or 0),
        "span_count": int(agg["span_count"] or 0),
        "avg_trace_latency_ms": round(float(agg["avg_trace_latency_ms"] or 412.5), 2),
        "ledger_integrity_valid": integrity["valid"],
        "ledger_verified_records": integrity["verified_records"],
        "ledger_head_hash": integrity["head_hash"],
        "per_case_costs": case_costs,
        "price_card": ModelCostEngine.PRICE_CARD_PER_1M,
    }


def seed_compliance_telemetry_if_empty(db_path: Optional[str] = None) -> None:
    """
    Seed baseline trace waterfalls and tamper-evident SEC/FinCEN ledger records
    for the initial corporate cases so the UI and MCP resources have rich data on startup.
    """
    ensure_compliance_tables(db_path)
    with _LEDGER_LOCK:
        conn = get_wal_sqlite_connection(db_path)
        try:
            existing_ledger = conn.execute(
                "SELECT COUNT(*) FROM sec_fincen_audit_ledger"
            ).fetchone()[0]
        finally:
            conn.close()

    if existing_ledger and int(existing_ledger) > 0:
        return

    now = datetime.now(timezone.utc)
    seed_cases = [
        ("NCZ-2024-8841", "Apex Global Commodities Ltd", "5493001KJTIIGC8Y1R12", "Alistair Finch", "a.finch@apexcommodities.co.uk", "10 Fenchurch Avenue, London EC3M 5BN", 2),
        ("NCZ-2024-8849", "Aethelgard Global Holdings Ltd", "549300V9N87YTR2P0431", "Marcus Vance", "m.vance@aethelgard-holdings.co.uk", "40 Bank Street, Canary Wharf, London E14 5NR", 2),
        ("NCZ-2024-8854", "Meridian Capital Partners Fund IV", "213800K98H45LMT2691", "Helena Vance", "h.vance@meridiancap.co.uk", "25 Berkeley Square, Mayfair, London W1J 6HN", 1),
        ("NCZ-2024-8862", "Vanguard Logistics Nordics AS", "5967007LIEEXZX890122", "Henrik Solberg", "h.solberg@vanguardnordics.no", "Aker Brygge 14, 0250 Oslo, Norway", 1),
        ("NCZ-2024-8833", "Sovereign Ridge Capital LLC", "549300ABC987XYZ12345", "Claire Sterling", "c.sterling@sovereignridge.com", "350 Madison Avenue, New York, NY 10017", 2),
        ("NCZ-2024-8802", "Albion Trade Finance PLC", "213800ALBION77654321", "David Thorne", "d.thorne@albiontrade.co.uk", "1 Poultry, London EC2R 8EJ", 2),
    ]

    for idx, (case_code, client_name, lei, contact_name, email, address, rnd) in enumerate(seed_cases):
        t_base = now - timedelta(hours=(18 - idx * 2))
        trace_id = hashlib.sha256(f"seed-trace-{case_code}-{rnd}".encode("utf-8")).hexdigest()[:32]
        root_span_id = hashlib.sha256(f"root-{trace_id}".encode("utf-8")).hexdigest()[:16]
        orch_span_id = hashlib.sha256(f"orch-{trace_id}".encode("utf-8")).hexdigest()[:16]
        draft_span_id = hashlib.sha256(f"draft-{trace_id}".encode("utf-8")).hexdigest()[:16]
        chat_draft_span_id = hashlib.sha256(f"chat-draft-{trace_id}".encode("utf-8")).hexdigest()[:16]
        critic_span_id = hashlib.sha256(f"critic-{trace_id}".encode("utf-8")).hexdigest()[:16]
        chat_critic_span_id = hashlib.sha256(f"chat-critic-{trace_id}".encode("utf-8")).hexdigest()[:16]

        in_tok_1, out_tok_1, cache_1 = 1840 + idx * 110, 620 + idx * 35, 640
        cost_1 = ModelCostEngine.compute_cost_usd("gemini-2.5-pro", in_tok_1, out_tok_1, cache_1)

        in_tok_2, out_tok_2, cache_2 = 1420 + idx * 80, 380 + idx * 25, 512
        cost_2 = ModelCostEngine.compute_cost_usd("gemini-2.5-pro", in_tok_2, out_tok_2, cache_2)

        total_cost = round(cost_1 + cost_2, 6)

        # 1. Root MCP / Pipeline span
        insert_trace_span(
            span_id=root_span_id,
            trace_id=trace_id,
            parent_span_id=None,
            case_id=case_code,
            round_number=rnd,
            span_name="mcp.tools/call.run_chase_pipeline",
            operation_name="execute_tool",
            provider_name="internal",
            model_name="fastmcp-v3-router",
            input_tokens=0,
            output_tokens=0,
            cached_tokens=0,
            cost_usd=0.0,
            latency_ms=442.8 + idx * 19.4,
            status="OK",
            attributes={
                "gen_ai.operation.name": "execute_tool",
                "gen_ai.provider.name": "internal",
                "gen_ai.request.model": "gemini-2.5-pro",
                "gen_ai.client.total_trace_cost.usd": total_cost,
                "kyc.case_id": case_code,
                "kyc.lei": lei,
                "kyc.round_number": rnd,
                "kyc.actor_id": "Sarah Jenkins (VP Corporate Banking)",
            },
            started_at=t_base.isoformat(),
            ended_at=(t_base + timedelta(milliseconds=443)).isoformat(),
            db_path=db_path,
        )

        # 2. Orchestrator DB Query Span
        insert_trace_span(
            span_id=orch_span_id,
            trace_id=trace_id,
            parent_span_id=root_span_id,
            case_id=case_code,
            round_number=rnd,
            span_name="langgraph.orchestrator_sla_scan",
            operation_name="db_query",
            provider_name="internal",
            model_name="sqlite-wal-engine",
            input_tokens=0,
            output_tokens=0,
            cached_tokens=0,
            cost_usd=0.0,
            latency_ms=18.4,
            status="OK",
            attributes={
                "kyc.case_id": case_code,
                "kyc.lei": lei,
                "db.system": "sqlite",
                "db.operation": "SELECT kyc_cases, kyc_documents",
            },
            started_at=(t_base + timedelta(milliseconds=5)).isoformat(),
            ended_at=(t_base + timedelta(milliseconds=24)).isoformat(),
            db_path=db_path,
        )

        # 3. AI Drafting Agent Span
        insert_trace_span(
            span_id=draft_span_id,
            trace_id=trace_id,
            parent_span_id=root_span_id,
            case_id=case_code,
            round_number=rnd,
            span_name="langgraph.ai_drafting_agent",
            operation_name="chat",
            provider_name="google_vertex",
            model_name="gemini-2.5-pro",
            input_tokens=in_tok_1,
            output_tokens=out_tok_1,
            cached_tokens=cache_1,
            cost_usd=cost_1,
            latency_ms=246.2,
            status="OK",
            attributes={
                "gen_ai.operation.name": "chat",
                "gen_ai.provider.name": "google_vertex",
                "gen_ai.request.model": "gemini-2.5-pro",
                "gen_ai.response.model": "gemini-2.5-pro-002",
                "gen_ai.usage.input_tokens": in_tok_1,
                "gen_ai.usage.output_tokens": out_tok_1,
                "gen_ai.client.cost.usd": cost_1,
                "kyc.case_id": case_code,
                "kyc.lei": lei,
                "kyc.contact": contact_name,
            },
            started_at=(t_base + timedelta(milliseconds=25)).isoformat(),
            ended_at=(t_base + timedelta(milliseconds=271)).isoformat(),
            db_path=db_path,
        )

        # 4. Critic Compliance QA Span
        insert_trace_span(
            span_id=critic_span_id,
            trace_id=trace_id,
            parent_span_id=root_span_id,
            case_id=case_code,
            round_number=rnd,
            span_name="langgraph.critic_compliance_qa",
            operation_name="chat",
            provider_name="google_vertex",
            model_name="gemini-2.5-pro",
            input_tokens=in_tok_2,
            output_tokens=out_tok_2,
            cached_tokens=cache_2,
            cost_usd=cost_2,
            latency_ms=168.4,
            status="OK",
            attributes={
                "gen_ai.operation.name": "chat",
                "gen_ai.provider.name": "google_vertex",
                "gen_ai.request.model": "gemini-2.5-pro",
                "gen_ai.response.model": "gemini-2.5-pro-002",
                "gen_ai.usage.input_tokens": in_tok_2,
                "gen_ai.usage.output_tokens": out_tok_2,
                "gen_ai.client.cost.usd": cost_2,
                "kyc.case_id": case_code,
                "kyc.lei": lei,
                "kyc.critic.legal_score": 0.99,
                "kyc.critic.politeness_score": 0.98,
            },
            started_at=(t_base + timedelta(milliseconds=272)).isoformat(),
            ended_at=(t_base + timedelta(milliseconds=440)).isoformat(),
            db_path=db_path,
        )

        # Append tamper-evident SEC/FinCEN ledger entries for this case
        append_audit_ledger_entry(
            trace_id=trace_id,
            case_id=case_code,
            actor_id="Sarah Jenkins (VP Corporate Banking)",
            actor_role="MCP_AGENT",
            entry_channel="MCP_STREAMABLE_HTTP",
            action_type="DRAFT_GENERATED",
            raw_input=f"Case={case_code} Entity={client_name} LEI={lei} UBO={contact_name} Email={email} Address={address}",
            raw_output=f"Dear {contact_name}, Synthesized grounded institutional KYC chase draft for {client_name} (LEI {lei}) via encrypted portal.",
            critic_legal_score=0.99,
            critic_politeness_score=0.98,
            critic_findings=[{"check": "SEC_17a4_FinCEN_CDD", "passed": True}],
            created_at=(t_base + timedelta(milliseconds=270)).isoformat(),
            db_path=db_path,
        )

        append_audit_ledger_entry(
            trace_id=trace_id,
            case_id=case_code,
            actor_id="system:critic_qa",
            actor_role="MCP_AGENT",
            entry_channel="MCP_STREAMABLE_HTTP",
            action_type="CRITIC_QA_PASSED",
            raw_input=f"Auditing draft for {case_code} ({client_name}, LEI {lei}) addressed to {contact_name} <{email}>",
            raw_output=f"All 5 PRA/FCA/SEC/FinCEN compliance guardrails passed for {case_code}. Ready for RM HITL sign-off.",
            critic_legal_score=0.99,
            critic_politeness_score=0.98,
            critic_findings=[
                {"key": "tone", "passed": True, "score": 0.98},
                {"key": "accuracy", "passed": True, "score": 1.0},
                {"key": "promises", "passed": True, "score": 1.0},
                {"key": "pii", "passed": True, "score": 1.0},
                {"key": "disclosures", "passed": True, "score": 0.99},
            ],
            created_at=(t_base + timedelta(milliseconds=440)).isoformat(),
            db_path=db_path,
        )

        if case_code == "NCZ-2024-8802":
            sig_ts = (t_base + timedelta(minutes=2)).isoformat()
            sample_body = f"Finalized KYC chase dispatch for {client_name} ({case_code})"
            sig = compute_hitl_hmac_signature(
                case_id=case_code,
                rm_id="Sarah Jenkins (VP Corporate Banking)",
                final_body=sample_body,
                timestamp_iso=sig_ts,
            )
            append_audit_ledger_entry(
                trace_id=trace_id,
                case_id=case_code,
                actor_id="Sarah Jenkins (VP Corporate Banking)",
                actor_role="RM_APPROVER",
                entry_channel="WEB_CONSOLE",
                action_type="RM_HITL_APPROVED",
                raw_input=f"RM sign-off for {case_code} recipient {contact_name} <{email}>",
                raw_output=sample_body,
                critic_legal_score=1.0,
                critic_politeness_score=0.99,
                hitl_rm_id="Sarah Jenkins (VP Corporate Banking)",
                hitl_signature=sig,
                created_at=sig_ts,
                db_path=db_path,
            )
