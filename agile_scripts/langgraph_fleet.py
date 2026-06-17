import os
import subprocess
import argparse
import logging
import re as _re
import json as _json
from typing import TypedDict, Annotated, List, Dict
from pydantic import BaseModel, Field
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage, AIMessage
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langchain_openai import ChatOpenAI
from openai import RateLimitError, APIStatusError
from atlassian import Jira

logger = logging.getLogger(__name__)

# ===========================================================================
# 1. Credenciales — inyectadas como env vars, nunca hardcodeadas
# ===========================================================================
MINIMAX_API_KEY    = os.getenv("MINIMAX_API_KEY")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
JIRA_URL           = os.getenv("JIRA_URL")
JIRA_USERNAME      = os.getenv("JIRA_USER")
JIRA_API_TOKEN     = os.getenv("JIRA_API_TOKEN")


def _is_quota_error(exc: Exception) -> bool:
    if isinstance(exc, RateLimitError):
        return True
    if isinstance(exc, APIStatusError) and exc.status_code in (402, 403, 429, 500, 502, 503, 529):
        return True
    msg = str(exc).lower()
    return any(kw in msg for kw in ("429", "rate limit", "quota", "overload", "529", "capacity", "unavailable"))


# ---------------------------------------------------------------------------
# Modelos primarios (MiniMax)
# ---------------------------------------------------------------------------
_minimax_dev = ChatOpenAI(
    api_key=MINIMAX_API_KEY,
    base_url="https://api.minimax.io/v1",
    model="MiniMax-M2.7",
    temperature=0.3,
    max_tokens=8192,
)

_minimax_reviewer = ChatOpenAI(
    api_key=MINIMAX_API_KEY,
    base_url="https://api.minimax.io/v1",
    model="MiniMax-M2.7",   # M2.7 es mas rapido y disponible que M3
    temperature=0.0,
    max_tokens=4096,
)

# ---------------------------------------------------------------------------
# Fallback OpenRouter — cadena de modelos gratuitos (sin Claude/Anthropic)
# ---------------------------------------------------------------------------
def _make_or(model: str, temperature: float) -> ChatOpenAI:
    return ChatOpenAI(
        api_key=OPENROUTER_API_KEY,
        base_url="https://openrouter.ai/api/v1",
        model=model,
        temperature=temperature,
        max_tokens=8192,
        default_headers={
            "HTTP-Referer": "https://veracta.atlassian.net",
            "X-Title": "Veracta LangGraph Fleet",
        },
    )

_OR_DEV_CHAIN = [
    _make_or("qwen/qwen3-coder:free", 0.3),
    _make_or("nvidia/nemotron-3-ultra-550b-a55b:free", 0.3),
    _make_or("nvidia/nemotron-3-super-120b-a12b:free", 0.3),
    _make_or("meta-llama/llama-3.3-70b-instruct:free", 0.3),
]

_OR_REVIEWER_CHAIN = [
    _make_or("nvidia/nemotron-3-super-120b-a12b:free", 0.0),
    _make_or("nvidia/nemotron-3-ultra-550b-a55b:free", 0.0),
    _make_or("meta-llama/llama-3.3-70b-instruct:free", 0.0),
]


def _invoke_chain(primary, fallback_chain: list, messages: list) -> object:
    candidates = [primary] + fallback_chain
    last_exc = None
    for model in candidates:
        try:
            return model.invoke(messages)
        except Exception as exc:
            if _is_quota_error(exc) or "404" in str(exc):
                name = getattr(model, "model_name", str(model))
                logger.warning("Modelo %s no disponible -> siguiente fallback", name)
                last_exc = exc
                continue
            raise
    raise RuntimeError(f"Todos los modelos fallaron. Ultimo error: {last_exc}") from last_exc


def _invoke_dev(messages: list) -> object:
    return _invoke_chain(_minimax_dev, _OR_DEV_CHAIN, messages)


def _invoke_reviewer(messages: list) -> object:
    return _invoke_chain(_minimax_reviewer, _OR_REVIEWER_CHAIN, messages)


def _extract_json(text: str) -> str:
    """Extrae el primer objeto JSON valido usando raw_decode (robusto ante strings con llaves)."""
    text = _re.sub(r"<think>.*?</think>", "", text, flags=_re.DOTALL).strip()
    md = _re.search(r"```(?:json)?\s*(\{.*?)\s*```", text, _re.DOTALL)
    if md:
        candidate = md.group(1)
        try:
            _json.loads(candidate)
            return candidate
        except _json.JSONDecodeError:
            pass
    decoder = _json.JSONDecoder()
    start = text.find("{")
    if start == -1:
        raise ValueError(f"No se encontro JSON en: {text[:200]}")
    while start < len(text):
        try:
            obj, _ = decoder.raw_decode(text, start)
            return _json.dumps(obj)
        except _json.JSONDecodeError:
            start = text.find("{", start + 1)
            if start == -1:
                break
    raise ValueError(f"No se encontro JSON valido en: {text[:200]}")


def _invoke_reviewer_structured(messages: list) -> "ReviewerDecision":
    raw = _invoke_chain(_minimax_reviewer, _OR_REVIEWER_CHAIN, messages)
    content = raw.content if hasattr(raw, "content") else str(raw)
    return ReviewerDecision(**_json.loads(_extract_json(content)))


jira_client = Jira(
    url=JIRA_URL,
    username=JIRA_USERNAME,
    password=JIRA_API_TOKEN,
    cloud=True,
)

# ===========================================================================
# 2. Helpers de workspace real
# ===========================================================================
_WORKSPACE_EXTENSIONS = (".rb", ".yml", ".yaml", ".erb", ".json", ".js", ".ts", ".html", ".haml")
_WORKSPACE_EXCLUDE    = ("vendor", ".git", "log", "tmp", "node_modules", "coverage", "public/assets")


def _get_workspace_context(workspace: str, hint_dirs: list = None,
                           max_files: int = 15, max_bytes: int = 3000) -> str:
    """
    Lee archivos relevantes del workspace. Prioriza hint_dirs si se proveen
    (ej: ['langgraph_runner', 'dispatcher']) para tickets focalizados.
    """
    context_parts = [f"# Workspace: {workspace}"]
    try:
        result = subprocess.run(
            ["find", workspace, "-type", "f"],
            capture_output=True, text=True, timeout=15,
        )
        all_files = [f for f in result.stdout.strip().split("\n") if f]

        def _relevant(path: str) -> bool:
            if any(excl in path for excl in _WORKSPACE_EXCLUDE):
                return False
            return any(path.endswith(ext) for ext in _WORKSPACE_EXTENSIONS)

        relevant = [f for f in all_files if _relevant(f)]

        if hint_dirs:
            # Priorizar archivos en los directorios indicados
            priority = [f for f in relevant if any(d in f for d in hint_dirs)]
            others   = [f for f in relevant if f not in priority]
            ordered  = priority[:max_files] + others[: max(0, max_files - len(priority))]
        else:
            ordered = relevant[:max_files]

        for fpath in ordered:
            try:
                rel = os.path.relpath(fpath, workspace)
                with open(fpath, "r", errors="replace") as fh:
                    content = fh.read(max_bytes)
                context_parts.append(f"\n===FILE_CONTEXT: {rel}===\n{content}\n===END===")
            except Exception as e:
                logger.debug("No se pudo leer %s: %s", fpath, e)

        # Listar también el árbol completo de archivos (sin contenido) para que el modelo
        # sepa qué existe aunque no lo lea completamente
        all_rel = [os.path.relpath(f, workspace) for f in relevant]
        context_parts.append(f"\n# Listado completo ({len(all_rel)} archivos):\n" + "\n".join(all_rel))

    except Exception as e:
        logger.warning("Error listando workspace: %s", e)
    return "\n".join(context_parts)


def _apply_workspace_changes(workspace: str, llm_response: str) -> list:
    """Extrae bloques ===FILE_BEGIN/END=== de la respuesta del LLM y los escribe al disco."""
    applied = []
    # [^\n\r=]+ evita que el path capture newlines o el === de cierre
    # [ \t]*\r?\n? hace el salto de linea despues de === opcional
    pattern = r"===FILE_BEGIN:\s*([^\n\r=]+?)===[ \t]*\r?\n?(.*?)===FILE_END==="
    matches = _re.findall(pattern, llm_response, _re.DOTALL)
    for rel_path, content in matches:
        rel_path = rel_path.strip()
        # Si el modelo uso \n literal en lugar de newlines reales, desescapar
        if "\\n" in content and "\n" not in content:
            content = content.replace("\\n", "\n").replace("\\t", "\t").replace("\\r", "")
        full_path = os.path.join(workspace, rel_path)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, "w") as fh:
            fh.write(content)
        applied.append(rel_path)
        logger.info("Archivo escrito: %s", full_path)
    return applied


# ===========================================================================
# 3. Estado del grafo y esquemas Pydantic
# ===========================================================================
class FleetState(TypedDict):
    messages:            Annotated[List[BaseMessage], add_messages]
    ticket_id:           str
    workspace_path:      str
    acceptance_criteria: str
    required_agents:     List[str]
    current_code_diff:   Dict[str, str]
    applied_files:       List[str]   # rutas relativas escritas al disco en todos los ciclos
    reviewer_feedback:   str
    is_approved:         bool
    loop_iterations:     int


class ReviewerDecision(BaseModel):
    is_approved:         bool = Field(description="True si la solucion cumple los criterios de aceptacion.")
    corrective_feedback: str  = Field(description="Retroalimentacion correctiva si falla; resumen si aprueba.")


# ===========================================================================
# 4. Nodos del grafo
# ===========================================================================
def fetch_and_plan_node(state: FleetState) -> dict:
    ticket_id   = state["ticket_id"]
    issue_data  = jira_client.issue(ticket_id)
    summary     = issue_data["fields"]["summary"]
    description = issue_data["fields"]["description"] or ""
    labels      = issue_data["fields"].get("labels", [])

    required_agents = [lbl.split(":")[1] for lbl in labels if lbl.startswith("agent:")]
    if not required_agents:
        required_agents = ["Full-Stack"]

    context_string = f"TITULO: {summary}\n\nCRITERIOS DE ACEPTACION:\n{description}"
    return {
        "acceptance_criteria": context_string,
        "required_agents":     required_agents,
        "is_approved":         False,
        "loop_iterations":     0,
        "current_code_diff":   {},
        "applied_files":       [],
        "messages":            [AIMessage(content=f"Agentes requeridos: {required_agents}", name="Planner")],
    }


def dynamic_developer_node(state: FleetState) -> dict:
    """Propone e implementa cambios REALES en el workspace montado."""
    workspace       = state["workspace_path"]
    criteria        = state["acceptance_criteria"]
    agents          = state["required_agents"]
    feedback        = state.get("reviewer_feedback", "Implementacion inicial requerida.")
    current_diff    = state.get("current_code_diff", {})
    prev_applied    = state.get("applied_files", [])

    # Detectar hint de directorio desde los criterios (ej: "langgraph_runner", "dispatcher")
    hint_dirs = []
    for kw in ["langgraph_runner", "dispatcher", "runner", "worker", "service", "lib"]:
        if kw in criteria.lower():
            hint_dirs.append(kw)

    workspace_context = _get_workspace_context(workspace, hint_dirs=hint_dirs or None)
    new_diffs   = {}
    all_applied = list(prev_applied)  # acumular entre ciclos

    FORMAT_EXAMPLE = (
        "FORMATO OBLIGATORIO — copia este patron para CADA archivo:\n\n"
        "===FILE_BEGIN: ruta/relativa/ejemplo.py===\n"
        "# contenido completo del archivo aqui\n"
        "print('hola mundo')\n"
        "===FILE_END===\n\n"
        "===FILE_BEGIN: otro/archivo.js===\n"
        "console.log('otro archivo');\n"
        "===FILE_END===\n"
    )

    for agent_role in agents:
        system_instruction = (
            f"Eres un experto en {agent_role}. Tu unica tarea es escribir codigo.\n"
            "NO expliques nada. NO describas lo que vas a hacer.\n"
            "SOLO escribe los archivos usando el formato especificado.\n\n"
            + FORMAT_EXAMPLE
        )
        human_instruction = (
            f"CRITERIOS DEL TICKET:\n{criteria}\n\n"
            f"RETROALIMENTACION DEL REVISOR:\n{feedback}\n\n"
            f"CONTEXTO DEL WORKSPACE:\n{workspace_context}\n\n"
            f"ARCHIVOS YA EN DISCO: {prev_applied}\n\n"
            "Escribe ahora TODOS los archivos necesarios usando bloques "
            "===FILE_BEGIN: ruta=== ... ===FILE_END===\n"
            "Empieza directamente con el primer ===FILE_BEGIN==="
        )

        response      = _invoke_dev([SystemMessage(content=system_instruction),
                                     HumanMessage(content=human_instruction)])
        response_text = response.content
        new_diffs[agent_role] = response_text

        try:
            applied = _apply_workspace_changes(workspace, response_text)
            # Acumular solo nuevos (evitar duplicados)
            for f in applied:
                if f not in all_applied:
                    all_applied.append(f)
            if applied:
                logger.info("Agente %s aplico cambios en: %s", agent_role, applied)
            else:
                logger.warning("Agente %s no genero bloques FILE_BEGIN/END", agent_role)
        except Exception as e:
            logger.error("Error aplicando cambios del agente %s: %s", agent_role, e)

    summary_msg = (
        f"Ciclo {state['loop_iterations'] + 1} completado. "
        f"Total archivos en disco: {len(all_applied)}"
    )
    return {
        "current_code_diff": new_diffs,
        "applied_files":     all_applied,
        "loop_iterations":   state["loop_iterations"] + 1,
        "messages":          [AIMessage(content=summary_msg, name="DevFleet")],
    }


def _read_applied_files(workspace: str, applied_files: list, max_bytes: int = 3000) -> str:
    """Lee el contenido actual de los archivos que realmente fueron escritos al disco."""
    if not applied_files:
        return "(ningún archivo fue escrito al disco)"
    parts = [f"# Archivos creados/modificados ({len(applied_files)} total):"]
    for rel_path in applied_files[:40]:  # max 40 archivos al revisor
        full_path = os.path.join(workspace, rel_path)
        try:
            with open(full_path, "r", errors="replace") as fh:
                content = fh.read(max_bytes)
            parts.append(f"\n===FILE: {rel_path}===\n{content}\n===END===")
        except Exception as e:
            parts.append(f"\n[No se pudo leer {rel_path}: {e}]")
    return "\n".join(parts)


def reviewer_node(state: FleetState) -> dict:
    """Evalua si los archivos escritos en el workspace cumplen los criterios de aceptacion."""
    workspace      = state["workspace_path"]
    criteria       = state["acceptance_criteria"]
    applied_files  = state.get("applied_files", [])

    # Fast-reject: si el desarrollador no escribió ningún archivo, no llamar al LLM
    if not applied_files:
        logger.warning("Reviewer fast-reject: el desarrollador no generó ningún archivo en esta iteración.")
        return {
            "reviewer_feedback": "El desarrollador no generó ningún archivo con el formato FILE_BEGIN/END. Debes usar EXACTAMENTE el formato especificado y escribir TODOS los archivos del servicio.",
            "is_approved": False,
        }

    # Leer el contenido REAL de los archivos escritos (no el workspace genérico)
    files_content = _read_applied_files(workspace, applied_files)

    sys_prompt = SystemMessage(
        content=(
            "Eres el Arquitecto Revisor. Analiza los archivos que el equipo de desarrollo "
            "escribio al disco y determina si cumplen los criterios de aceptacion del ticket.\n\n"
            "IMPORTANTE: Responde UNICAMENTE con un objeto JSON valido, sin texto adicional, "
            "sin markdown, sin bloques de codigo. El objeto debe tener exactamente estos campos:\n"
            '{"is_approved": <true|false>, "corrective_feedback": "<texto>"}'
        )
    )
    human_prompt = HumanMessage(
        content=(
            f"CRITERIOS DE ACEPTACION:\n{criteria}\n\n"
            f"ARCHIVOS ESCRITOS AL DISCO:\n{files_content}\n\n"
            "Emite tu dictamen en JSON."
        )
    )

    decision: ReviewerDecision = _invoke_reviewer_structured([sys_prompt, human_prompt])
    return {
        "reviewer_feedback": decision.corrective_feedback,
        "is_approved":       decision.is_approved,
        "messages": [
            AIMessage(
                content=f"{'APROBADO' if decision.is_approved else 'RECHAZADO'}: {decision.corrective_feedback}",
                name="Reviewer",
            )
        ],
    }


def finalize_and_update_jira(state: FleetState) -> dict:
    ticket_id    = state["ticket_id"]
    feedback     = state["reviewer_feedback"]
    iterations   = state["loop_iterations"]
    applied      = state.get("applied_files", [])
    status_parts = []

    # Comentar en Jira (no fatal si falla por permisos)
    comment = (
        f"Implementacion procesada por la Flota de Agentes\n\n"
        f"Ciclos de revision: {iterations}\n"
        f"Archivos modificados: {len(applied)}\n\n"
        f"Resultado del revisor:\n{feedback}"
    )
    try:
        jira_client.issue_add_comment(ticket_id, comment)
        status_parts.append("comentario añadido")
    except Exception as e:
        logger.warning("No se pudo comentar en %s: %s", ticket_id, e)
        status_parts.append(f"comentario omitido ({e})")

    # Transicionar el ticket
    try:
        transitions     = jira_client.get_issue_transitions(ticket_id)
        target_keywords = ["done", "resolved", "in review", "closed", "hecho", "resuelto"]
        for transition in transitions:
            to_value    = transition.get("to", "")
            target_name = (to_value if isinstance(to_value, str) else to_value.get("name", "")).lower()
            if any(kw in target_name for kw in target_keywords):
                jira_client.transition_issue(ticket_id, str(transition["id"]))
                status_parts.append(f"transicionado a '{target_name}'")
                break
    except Exception as e:
        logger.warning("No se pudo transicionar %s: %s", ticket_id, e)
        status_parts.append(f"transicion omitida ({e})")

    msg = "Jira: " + "; ".join(status_parts) if status_parts else "Jira actualizado."
    return {"messages": [AIMessage(content=msg, name="JiraOps")]}


# ===========================================================================
# 5. Enrutador condicional (quality gate)
# ===========================================================================
def quality_gate_router(state: FleetState) -> str:
    if state["loop_iterations"] >= 6 or state["is_approved"]:
        return "jira_updater"
    return "dynamic_developer"


# ===========================================================================
# 6. Construccion del grafo
# ===========================================================================
def build_architecture() -> StateGraph:
    graph = StateGraph(FleetState)
    graph.add_node("context_ingestion", fetch_and_plan_node)
    graph.add_node("dynamic_developer", dynamic_developer_node)
    graph.add_node("quality_reviewer",  reviewer_node)
    graph.add_node("jira_updater",      finalize_and_update_jira)

    graph.add_edge(START, "context_ingestion")
    graph.add_edge("context_ingestion", "dynamic_developer")
    graph.add_edge("dynamic_developer", "quality_reviewer")
    graph.add_conditional_edges(
        "quality_reviewer",
        quality_gate_router,
        {"dynamic_developer": "dynamic_developer", "jira_updater": "jira_updater"},
    )
    graph.add_edge("jira_updater", END)
    return graph.compile()


# ===========================================================================
# 7. Entrypoint — invocado por fleet_api.py o directo via CLI
# ===========================================================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Flota multi-agente LangGraph para tickets de Jira")
    parser.add_argument("--ticket",    required=True, help="ID del ticket de Jira (ej. SCRUM-28)")
    parser.add_argument("--workspace", required=True, help="Ruta al directorio del proyecto")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    engine = build_architecture()
    initial_payload: FleetState = {
        "messages":            [],
        "ticket_id":           args.ticket,
        "workspace_path":      args.workspace,
        "acceptance_criteria": "",
        "required_agents":     [],
        "current_code_diff":   {},
        "applied_files":       [],
        "reviewer_feedback":   "",
        "is_approved":         False,
        "loop_iterations":     0,
    }

    print(f"\nIniciando flota para ticket: {args.ticket}")
    for step_event in engine.stream(initial_payload, stream_mode="updates"):
        for node_name, data in step_event.items():
            if data.get("messages"):
                print(f"\n[{node_name}] -> {data['messages'][-1].content}")

    print("\nFlota completada.")
