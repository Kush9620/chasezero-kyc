from __future__ import annotations

import contextvars
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from .compliance_ledger import PIIRedactor, insert_trace_span

# Context variables propagating RM identity, entry channel, and active KYC case across MCP -> LangGraph -> SQLite
current_actor_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "current_actor_id", default="Sarah Jenkins (VP Corporate Banking)"
)
current_actor_role_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "current_actor_role", default="RM_APPROVER"
)
current_entry_channel_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "current_entry_channel", default="WEB_CONSOLE"
)
current_case_id_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "current_case_id", default=None
)
current_round_var: contextvars.ContextVar[int] = contextvars.ContextVar(
    "current_round", default=1
)


def set_execution_context(
    *,
    actor_id: Optional[str] = None,
    actor_role: Optional[str] = None,
    entry_channel: Optional[str] = None,
    case_id: Optional[str] = None,
    round_number: Optional[int] = None,
) -> None:
    if actor_id is not None:
        current_actor_id_var.set(actor_id)
    if actor_role is not None:
        current_actor_role_var.set(actor_role)
    if entry_channel is not None:
        current_entry_channel_var.set(entry_channel)
    if case_id is not None:
        current_case_id_var.set(case_id)
    if round_number is not None:
        current_round_var.set(int(round_number))


def get_execution_context() -> Dict[str, Any]:
    return {
        "actor_id": current_actor_id_var.get(),
        "actor_role": current_actor_role_var.get(),
        "entry_channel": current_entry_channel_var.get(),
        "case_id": current_case_id_var.get(),
        "round_number": current_round_var.get(),
    }


def _ns_to_iso(ns: Optional[int]) -> str:
    if not ns:
        return datetime.now(timezone.utc).isoformat()
    return datetime.fromtimestamp(ns / 1_000_000_000.0, tz=timezone.utc).isoformat()


class SQLiteSpanPersistenceProcessor(SpanProcessor):
    """
    OpenTelemetry SpanProcessor that captures finished spans (mcp.*, langgraph.*, gen_ai.*, sqlite.*)
    and persists them directly into SQLite `llm_trace_spans` with deterministic PII redaction.
    """

    RELEVANT_PREFIXES = ("mcp.", "langgraph.", "gen_ai.", "sqlite.", "kyc.")

    def on_start(self, span: Any, parent_context: Optional[Any] = None) -> None:
        case_id = current_case_id_var.get()
        actor_id = current_actor_id_var.get()
        rnd = current_round_var.get()
        if case_id and span.is_recording():
            span.set_attribute("kyc.case_id", case_id)
            span.set_attribute("kyc.round_number", rnd)
        if actor_id and span.is_recording():
            span.set_attribute("kyc.actor_id", actor_id)

    def on_end(self, span: ReadableSpan) -> None:
        try:
            name = span.name or ""
            attrs = dict(span.attributes or {})
            is_relevant = (
                name.startswith(self.RELEVANT_PREFIXES)
                or "gen_ai.operation.name" in attrs
                or "kyc.case_id" in attrs
            )
            if not is_relevant:
                return

            span_ctx = span.get_span_context()
            trace_id_hex = format(span_ctx.trace_id, "032x")
            span_id_hex = format(span_ctx.span_id, "016x")
            parent_id_hex = (
                format(span.parent.span_id, "016x")
                if span.parent and span.parent.is_valid
                else None
            )

            case_id = str(attrs.get("kyc.case_id") or current_case_id_var.get() or "") or None
            round_num = int(attrs.get("kyc.round_number") or current_round_var.get() or 1)
            op_name = str(
                attrs.get("gen_ai.operation.name")
                or ("execute_tool" if name.startswith("mcp.") else "invoke_agent")
            )
            provider = str(attrs.get("gen_ai.provider.name") or "internal")
            model = (
                str(attrs.get("gen_ai.request.model") or attrs.get("gen_ai.response.model") or "")
                or None
            )
            in_tok = int(attrs.get("gen_ai.usage.input_tokens") or 0)
            out_tok = int(attrs.get("gen_ai.usage.output_tokens") or 0)
            cache_tok = int(attrs.get("gen_ai.usage.cached_tokens") or 0)
            cost_usd = float(attrs.get("gen_ai.client.cost.usd") or 0.0)

            start_ns = span.start_time or 0
            end_ns = span.end_time or start_ns
            latency_ms = max(0.05, (end_ns - start_ns) / 1_000_000.0)

            custom_status = attrs.get("kyc.span.status")
            if custom_status:
                status_str = str(custom_status)
            elif span.status and span.status.status_code.name == "ERROR":
                status_str = "ERROR"
            else:
                status_str = "OK"

            err_msg = (
                str(attrs.get("error.message") or (span.status.description if span.status else "") or "")
                or None
            )

            sanitized_attrs = PIIRedactor.redact_obj(attrs)

            insert_trace_span(
                span_id=span_id_hex,
                trace_id=trace_id_hex,
                parent_span_id=parent_id_hex,
                case_id=case_id,
                round_number=round_num,
                span_name=name,
                operation_name=op_name,
                provider_name=provider,
                model_name=model,
                input_tokens=in_tok,
                output_tokens=out_tok,
                cached_tokens=cache_tok,
                cost_usd=cost_usd,
                latency_ms=latency_ms,
                status=status_str,
                error_message=err_msg,
                attributes=sanitized_attrs,
                started_at=_ns_to_iso(start_ns),
                ended_at=_ns_to_iso(end_ns),
            )
        except Exception:
            # Never allow telemetry export failure to crash application requests
            pass

    def shutdown(self) -> None:
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


_TRACER_PROVIDER: Optional[TracerProvider] = None
_SQLITE_PROCESSOR: Optional[SQLiteSpanPersistenceProcessor] = None


def init_telemetry() -> TracerProvider:
    global _TRACER_PROVIDER, _SQLITE_PROCESSOR
    if _TRACER_PROVIDER is not None:
        return _TRACER_PROVIDER

    resource = Resource.create(
        {
            "service.name": "northgate-chasezero",
            "service.version": "2.0.0",
            "deployment.environment": os.getenv("DEPLOYMENT_ENV", "production"),
        }
    )

    provider = TracerProvider(resource=resource)
    _SQLITE_PROCESSOR = SQLiteSpanPersistenceProcessor()
    provider.add_span_processor(_SQLITE_PROCESSOR)

    otlp_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if otlp_endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            otlp_exporter = OTLPSpanExporter(endpoint=otlp_endpoint)
            provider.add_span_processor(BatchSpanProcessor(otlp_exporter))
        except Exception:
            pass

    try:
        trace.set_tracer_provider(provider)
    except Exception:
        pass

    _TRACER_PROVIDER = provider
    return provider


def get_tracer(name: str = "northgate.chasezero") -> trace.Tracer:
    provider = init_telemetry()
    return provider.get_tracer(name, "2.0.0")


def get_current_trace_id_hex() -> str:
    span = trace.get_current_span()
    if span and span.get_span_context() and span.get_span_context().is_valid:
        return format(span.get_span_context().trace_id, "032x")
    return format(int.from_bytes(os.urandom(16), "big"), "032x")


def flush_and_shutdown_telemetry() -> None:
    global _TRACER_PROVIDER
    if _TRACER_PROVIDER is not None:
        try:
            _TRACER_PROVIDER.force_flush(timeout_millis=5000)
        except Exception:
            pass
