"""
Trazas OpenTelemetry para la flota (spec langgraph-hardening, Req 6).

Jerarquía: job_span (raíz, uno por job) → node_span (uno por nodo del grafo)
→ llm_span (uno por invocación de modelo). La anidación sale del contexto
OTel del thread: el grafo corre síncrono en el thread del worker, así que la
propagación implícita funciona sin plumbing.

Degradación garantizada (Req 6.2): si OTEL_EXPORTER_OTLP_ENDPOINT no está
definida, o el SDK de opentelemetry no está instalado (entorno local de
tests), todos los helpers son no-ops que nunca lanzan excepción.

Privacidad (Req 6.6): los atributos de spans NUNCA incluyen contenido de
prompts ni respuestas — solo nombres, contadores y duraciones. Por eso se
instrumenta a mano en vez de usar auto-instrumentación (que exporta prompts
completos por defecto).
"""
import os
from contextlib import contextmanager, nullcontext

try:
    from opentelemetry import trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    _OTEL_AVAILABLE = True
except ImportError:
    _OTEL_AVAILABLE = False

_tracer = None


def setup_tracing(service_name: str = "langgraph-fleet") -> None:
    """Configura el tracing una sola vez (llamado en el startup de fleet_api).

    Sin OTEL_EXPORTER_OTLP_ENDPOINT o sin el SDK instalado: no configura nada
    y los helpers quedan como no-ops (Req 6.2). Con endpoint: exporta OTLP
    HTTP en batch (Req 6.1)."""
    global _tracer
    if not _OTEL_AVAILABLE:
        return
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if not endpoint:
        return
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    _tracer = trace.get_tracer("fleet")


def set_tracer_for_tests(tracer) -> None:
    """Inyecta un tracer con exporter en memoria (solo tests)."""
    global _tracer
    _tracer = tracer


def _span(name: str, attributes: dict, kind=None):
    if _tracer is None:
        return nullcontext()
    if kind is None:
        return _tracer.start_as_current_span(name, attributes=attributes)
    return _tracer.start_as_current_span(name, attributes=attributes, kind=kind)


def job_span(job_id: str, ticket_id: str):
    """Span raíz de un job (uno por ejecución del FleetEngine).

    Marcado como SERVER (no INTERNAL) para que el tab Monitor (SPM) de Jaeger
    —que filtra por SPAN_KIND_SERVER por defecto— muestre la actividad de la
    flota (throughput, tasa de error y latencia por job) sin cambiar el filtro.
    Los spans hijos node.* y llm.* siguen siendo INTERNAL."""
    server_kind = trace.SpanKind.SERVER if _OTEL_AVAILABLE else None
    return _span(
        "fleet.job",
        {"fleet.job_id": job_id, "fleet.ticket_id": ticket_id},
        kind=server_kind,
    )


def node_span(node_name: str):
    """Span de un nodo del grafo, anidado bajo el job (Req 6.3)."""
    return _span(f"node.{node_name}", {"fleet.node": node_name})


def llm_span(model_name: str, cycle: int):
    """Span de una invocación de modelo (Req 6.4). Atributos permitidos:
    modelo, ciclo y tokens (vía set_llm_span_tokens). Nunca contenido."""
    return _span("llm.invoke", {"llm.model_name": model_name, "fleet.cycle": cycle})


def set_llm_span_tokens(span, input_tokens: int, output_tokens: int) -> None:
    """Agrega los contadores de tokens al span de la invocación (Req 6.4).
    `span` es None cuando el tracing está deshabilitado (nullcontext)."""
    if span is None:
        return
    try:
        span.set_attribute("llm.input_tokens", int(input_tokens))
        span.set_attribute("llm.output_tokens", int(output_tokens))
    except Exception:  # noqa: BLE001 — nunca romper un job por telemetría
        pass
