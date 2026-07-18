"""
Spec langgraph-hardening — Req 6 (trazas OpenTelemetry).

El test de degradación (6.2) corre en cualquier entorno (sin el SDK
instalado, fleet_tracing debe ser no-op sin excepción). Los tests de
jerarquía/atributos requieren el SDK real y corren dentro del contenedor.
"""
import os
import sys

import pytest

_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "agile_scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import fleet_tracing  # noqa: E402


def test_sin_endpoint_los_helpers_son_noop_sin_excepcion(monkeypatch):
    """Req 6.2: sin OTEL_EXPORTER_OTLP_ENDPOINT (o sin el SDK instalado), los
    jobs corren sin exportar spans y sin lanzar excepción."""
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.setattr(fleet_tracing, "_tracer", None)

    fleet_tracing.setup_tracing()
    assert fleet_tracing._tracer is None

    with fleet_tracing.job_span("j1", "T-1") as job:
        with fleet_tracing.node_span("planner"):
            with fleet_tracing.llm_span("modelo-x", 1) as span:
                fleet_tracing.set_llm_span_tokens(span, 10, 20)
    assert job is None  # nullcontext


def test_jerarquia_y_atributos_de_spans(monkeypatch):
    """Req 6.3/6.4/6.6: jerarquía job→nodo→llm; atributos solo de la lista
    permitida (nunca contenido de prompts/respuestas)."""
    pytest.importorskip("opentelemetry.sdk",
                        reason="requiere opentelemetry real (corre dentro del contenedor)")
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(fleet_tracing, "_tracer", provider.get_tracer("fleet-test"))

    with fleet_tracing.job_span("job-9", "TASK-9"):
        with fleet_tracing.node_span("dynamic_developer"):
            with fleet_tracing.llm_span("qwen-coder", 2) as span:
                fleet_tracing.set_llm_span_tokens(span, 111, 222)

    spans = {s.name: s for s in exporter.get_finished_spans()}
    assert set(spans) == {"fleet.job", "node.dynamic_developer", "llm.invoke"}

    job = spans["fleet.job"]
    node = spans["node.dynamic_developer"]
    llm = spans["llm.invoke"]

    # Jerarquía: llm hijo de node, node hijo de job (Req 6.3)
    assert node.parent.span_id == job.context.span_id
    assert llm.parent.span_id == node.context.span_id

    # Atributos del span LLM: exactamente los permitidos (Req 6.4/6.6)
    assert dict(llm.attributes) == {
        "llm.model_name": "qwen-coder",
        "fleet.cycle": 2,
        "llm.input_tokens": 111,
        "llm.output_tokens": 222,
    }
    assert dict(job.attributes) == {"fleet.job_id": "job-9", "fleet.ticket_id": "TASK-9"}
    assert dict(node.attributes) == {"fleet.node": "dynamic_developer"}
