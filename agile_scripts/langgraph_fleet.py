import os
import shutil
import subprocess
import argparse
import logging
import threading
import time
import difflib
import re as _re
import json as _json
from typing import TypedDict, Annotated, List, Dict, Optional, Callable
from pydantic import BaseModel, Field
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage, AIMessage
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langgraph.errors import GraphRecursionError, NodeError  # re-exportado para fleet_api
from langgraph.func import task
from langgraph.managed import RemainingSteps
from langgraph.types import RetryPolicy
from langchain_openai import ChatOpenAI
from openai import RateLimitError, APIStatusError
from atlassian import Jira

import fleet_tracing

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Callback de logging por hilo (thread-safe para jobs concurrentes)
# ---------------------------------------------------------------------------
_thread_local = threading.local()

def set_log_callback(fn: Callable[[str], None]) -> None:
    _thread_local.log_callback = fn

def _log(message: str) -> None:
    fn = getattr(_thread_local, "log_callback", None)
    if fn:
        try:
            fn(message)
        except Exception:
            pass

# ===========================================================================
# 1. Credenciales — inyectadas como env vars, nunca hardcodeadas
# ===========================================================================
MINIMAX_API_KEY    = os.getenv("MINIMAX_API_KEY")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
JIRA_URL           = os.getenv("JIRA_URL")
JIRA_USERNAME      = os.getenv("JIRA_USER")
JIRA_API_TOKEN     = os.getenv("JIRA_API_TOKEN")


# ---------------------------------------------------------------------------
# Checkpointer durable (spec langgraph-hardening, Req 1)
# ---------------------------------------------------------------------------
_checkpointer = None
_checkpointer_lock = threading.Lock()


def _get_checkpointer():
    """Singleton de SqliteSaver sobre el volumen persistente. Lazy para que
    importar este módulo (tests, tooling) no toque el filesystem."""
    global _checkpointer
    with _checkpointer_lock:
        if _checkpointer is None:
            import sqlite3
            from langgraph.checkpoint.sqlite import SqliteSaver
            path = os.getenv("FLEET_CHECKPOINT_DB", "/data/n8n_store/checkpoints.db")
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            # check_same_thread=False: los jobs corren en threads del
            # ThreadPoolExecutor de fleet_api; el lock interno del saver
            # serializa las escrituras.
            conn = sqlite3.connect(path, check_same_thread=False)
            _checkpointer = SqliteSaver(conn)
        return _checkpointer


def invoke_config(job_id: str) -> dict:
    """Config de invocación compartida por fleet_api y el CLI: el thread_id
    ata los checkpoints al job (Req 1.2) y el recursion_limit acota cualquier
    ejecución desbocada (Req 7.1; cota real del grafo ≈35 super-steps)."""
    return {"configurable": {"thread_id": job_id}, "recursion_limit": 60}


def delete_job_checkpoints(job_id: str) -> None:
    """Borra los checkpoints de un job terminado (Req 1.3) — cada checkpoint
    incluye el estado completo (con contenido de archivos); sin limpieza el
    archivo crecería sin cota. Best-effort: un fallo acá no debe romper el
    cierre del job."""
    try:
        _get_checkpointer().delete_thread(job_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("No se pudieron borrar los checkpoints de %s: %s", job_id, e)


def _is_quota_error(exc: Exception) -> bool:
    if isinstance(exc, RateLimitError):
        return True
    if isinstance(exc, APIStatusError) and exc.status_code in (402, 403, 429, 500, 502, 503, 529):
        return True
    msg = str(exc).lower()
    return any(kw in msg for kw in (
        "429", "rate limit", "quota", "overload", "529", "capacity", "unavailable",
        "resourceexhausted", "resource exhausted", "request limit", "worker local total request limit",
        "maximum context length", "context length", "context_length_exceeded",
    ))


# ---------------------------------------------------------------------------
# Modelos primarios (MiniMax)
# ---------------------------------------------------------------------------
_minimax_dev = ChatOpenAI(
    api_key=MINIMAX_API_KEY,
    base_url="https://api.minimax.io/v1",
    model="MiniMax-M2.7",
    temperature=0.3,
    max_tokens=40960,
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
def _make_or(model: str, temperature: float, timeout: int = 900) -> ChatOpenAI:
    return ChatOpenAI(
        api_key=OPENROUTER_API_KEY,
        base_url="https://openrouter.ai/api/v1",
        model=model,
        temperature=temperature,
        max_tokens=40960,
        request_timeout=timeout,
        default_headers={
            "HTTP-Referer": "https://veracta.atlassian.net",
            "X-Title": "Veracta LangGraph Fleet",
        },
    )

_OR_DEV_CHAIN = [
    _make_or("nvidia/nemotron-3-ultra-550b-a55b:free", 0.3),
    _make_or("nvidia/nemotron-3-super-120b-a12b:free", 0.3),
    _make_or("meta-llama/llama-3.3-70b-instruct:free", 0.3),
]

# Para desarrollo: Qwen 3 Coder como modelo primario (mejor seguimiento de formato FILE_BEGIN/END)
_qwen_dev = _make_or("qwen/qwen3-coder:free", 0.3)

_OR_REVIEWER_CHAIN = [
    _make_or("nvidia/nemotron-3-super-120b-a12b:free", 0.0),
    _make_or("nvidia/nemotron-3-ultra-550b-a55b:free", 0.0),
    _make_or("meta-llama/llama-3.3-70b-instruct:free", 0.0),
]


# ===========================================================================
# Guardrails de ingeniería — por stack. Se inyectan en CADA prompt del dev.
# ===========================================================================
ENGINEERING_GUARDRAILS_RAILS = """
REGLAS DE INGENIERÍA NO NEGOCIABLES (incumplirlas = rechazo automático):

[GROUNDING — la fuente de verdad existe; NO inventes]
- NUNCA escribas un modelo/clase desde la imaginación. Antes de tocar un modelo
  Rails, alinéate con TRES fuentes ya presentes en el repo y hazlas coincidir:
  (1) el SPEC del modelo (spec/models/*_spec.rb) = el contrato de comportamiento,
  (2) la FACTORY (spec/factories/*.rb) = los atributos/valores esperados,
  (3) el ESQUEMA real de la tabla (db/schema.rb y db/*migrate*/*create_*).
  Si los tres divergen, el SPEC manda; ajusta modelo+migración+factory a él.
- Usa EXACTAMENTE los nombres de columna que existen en la migración. No traduzcas
  inglés↔español (estado/status, accion/action, tipo/type) por tu cuenta.

[ACTIVE RECORD / ENUMS — causa #1 de crashes con eager_load]
- Todo `enum :campo` DEBE tener una columna backing en la tabla, o un
  `attribute :campo, :string|:integer` explícito ANTES del enum. Sin eso, en
  producción con eager_load=false explota: "Undeclared attribute type for enum".
- Modelos de TENANT heredan de TenantRecord; modelos de control de ApplicationRecord.
  Una tabla en db/tenant_migrate/ ⇒ el modelo es TenantRecord (NO ApplicationRecord).
  No declares belongs_to/has_many cruzando control↔tenant (conexiones distintas).

[MIGRACIONES]
- Toda columna que el modelo/scope/validación usa debe existir en una migración.
- enum integer ⇒ columna integer; enum string ⇒ columna string. Deben coincidir.

[TESTS — sin esto el trabajo NO está hecho]
- Cada spec de modelo debe iniciar con `require "rails_helper"` (no hay .rspec).
- Un cambio no está terminado hasta que su spec pasa Y el código parsea (`ruby -c`).
- No uses placeholders ('...', '# TODO', '# resto') — el archivo va completo.

[NO ROMPAS EL BOOT]
- No crees initializers que llamen a APIs de gemas inexistentes ni con sintaxis
  inválida. Si dudas de que una gema/clase exista, NO la uses.
""".strip()

ENGINEERING_GUARDRAILS_NODE = """
REGLAS DE INGENIERÍA NO NEGOCIABLES (incumplirlas = rechazo automático):

[STACK — este es un proyecto TypeScript/Next.js + Prisma. NO uses Ruby, Rails ni .rb]
- Lenguaje: TypeScript (.ts / .tsx). NUNCA generes archivos .rb, .erb, ni Ruby.
- ORM: Prisma (schema.prisma, prisma/migrations/). NO uses ActiveRecord ni migrate de Rails.
- Framework web: Next.js 14 App Router (src/app/). NO uses Rails controllers.
- Mobile: Expo/React Native. Archivos mobile SIEMPRE en mobile/ (ej: mobile/components/PhoneField.tsx,
  mobile/app/...). NUNCA en src/components/mobile/ ni src/app/mobile/. El tsconfig web excluye
  mobile/ — si pones código React Native en src/, tsc fallará por imports de react-native.

[GROUNDING — lee el código existente antes de escribir]
- Antes de crear un componente, busca patrones similares en src/components/.
- Antes de tocar Prisma, lee prisma/schema.prisma completo para conocer el esquema real.
- Usa EXACTAMENTE los nombres de campo que existen en el schema. No inventes campos.
- Verifica los imports existentes en el archivo antes de agregar nuevos.

[PRISMA / MIGRACIONES]
- Cambios al schema.prisma SIEMPRE van acompañados de su migración SQL en prisma/migrations/.
- El nombre de la migración: prisma/migrations/<timestamp>_<descripcion>/migration.sql
- Nunca generes un campo en el schema sin agregar la migración correspondiente.

[TYPESCRIPT — sin esto el trabajo NO está hecho]
- Todo archivo .ts/.tsx debe compilar sin errores (npx tsc --noEmit).
- No uses `any` sin justificación. Usa los tipos ya definidos en el proyecto.
- No uses placeholders ('...', '// TODO', '// rest of implementation') — el archivo va completo.
- Imports con alias @/ mapean a src/ (configurado en tsconfig.json).

[TDD — metodología obligatoria para todo cambio de código]
- Escribe los tests ANTES o JUNTO con el código de implementación (Red → Green → Refactor).
- Todo función pura nueva en src/lib/ o mobile/lib/ DEBE tener su archivo *.test.ts.
  Ejemplo: si creas src/lib/foo-utils.ts → crea src/lib/foo-utils.test.ts con describe+it.
- Los tests de unidad van en src/lib/*.test.ts y mobile/lib/*.test.ts (Vitest).
- Los tests de integración de rutas/actions van también en src/lib/ o src/server/ con Vitest.
- Los tests E2E de flujos de usuario van en tests/e2e/*.spec.ts (Playwright).
- Para tests de regresión: cuando modificas código existente, agrega al menos un test que
  verifique que el comportamiento anterior sigue funcionando.
- Framework de tests: `vitest` (ya instalado). Importa `{ describe, it, expect } from "vitest"`.
- Un cambio NO está terminado hasta que `npx vitest run` pasa con los nuevos tests incluidos.
- No uses placeholders en tests ('// TODO: test this'). Los tests deben ser reales y ejecutables.

[VERCEL: feature flags y rutas de staging]
- Para rutas o comportamiento que solo debe existir en Vercel Preview (staging), usa
  `process.env.VERCEL_ENV === "preview"` en lugar de variables custom como E2E_TEST_MODE.
  Vercel inyecta VERCEL_ENV automáticamente ("production" / "preview" / "development") sin
  configuración adicional. Las variables custom añadidas con `vercel env add` se guardan como
  Sensitive/Encrypted por defecto y pueden no llegar al runtime.
- Si creas endpoints o comportamiento gated por environment, verifica el check en TODOS los
  puntos de entrada: route handler, middleware, auth helpers, server context. Un check suelto
  en un solo lugar no es suficiente.
- Todo API route nuevo que dependa de server-side logic debe tener `export const dynamic =
  "force-dynamic"` para evitar que Next.js lo prerenderice como estático en el build.

[NO ROMPAS EL BUILD]
- No importes paquetes que no estén en package.json.
- No cambies next.config.mjs ni tsconfig.json sin necesidad.
- Los Server Components no pueden tener 'use client' ni usar hooks de React.
- Los Client Components deben tener 'use client' al inicio si usan useState/useEffect/hooks.
""".strip()

ENGINEERING_GUARDRAILS_GENERIC = """
REGLAS DE INGENIERÍA NO NEGOCIABLES (incumplirlas = rechazo automático):
- Escribe código completo y funcional. Sin placeholders ('...', 'TODO', 'rest of implementation').
- Respeta el stack y lenguaje del proyecto existente. No introduzcas lenguajes ajenos.
- Lee los archivos existentes antes de escribir para no romper interfaces ya definidas.
""".strip()

def _get_guardrails(stack: str) -> str:
    if stack == "rails":
        return ENGINEERING_GUARDRAILS_RAILS
    if stack == "node":
        return ENGINEERING_GUARDRAILS_NODE
    return ENGINEERING_GUARDRAILS_GENERIC

# Compatibilidad retroactiva
ENGINEERING_GUARDRAILS = ENGINEERING_GUARDRAILS_RAILS

# Playbooks por especialidad: refuerzo de rol además de las guardrails comunes.
ROLE_PLAYBOOKS = {
    "Rails":      "Enfócate en modelo+migración+spec coherentes. enum⇒columna/attribute. TenantRecord para tablas de tenant.",
    "Backend":    "Igual que Rails. Valida con rspec los archivos tocados. Controllers delgados.",
    "Schema":     "Eres el guardián del esquema: reconcilia spec↔factory↔tabla. Agrega columnas faltantes vía migración idempotente.",
    "Mobile":     "Flutter/Dart. Respeta el contrato de la API (campos snake_case del backend). No rompas el build (`flutter analyze`).",
    "Flutter":    "Flutter/Dart. Respeta el contrato de la API. Mantén el build verde (`flutter analyze`).",
    "Full-Stack": "Triangula spec↔factory↔esquema antes de codificar. Tras escribir, todo debe parsear y los specs tocados pasar.",
    "Node":       "Next.js 14 App Router + Prisma + TypeScript. Server Actions para mutaciones. Client Components con 'use client'. npx tsc --noEmit debe pasar.",
    "TypeScript": "TypeScript estricto. Tipos explícitos. Sin any. Imports con alias @/. Todo debe compilar.",
    "React":      "Componentes React funcionales con hooks. 'use client' cuando sea necesario. shadcn/ui para UI components.",
    "QA":         "Experto en calidad de software. Tu tarea es SOLO escribir tests: nunca modifiques el código de implementación. Cubre unit tests (src/lib/*.test.ts), integration tests (server actions y API routes), regression tests (comportamiento previo) y E2E (tests/e2e/*.spec.ts con Playwright). Sigue TDD: Red → Green → Refactor. Usa vitest para unit/integration, Playwright para E2E. Todos los tests deben ser ejecutables y pasar.",
}

ROLE_PLAYBOOKS_NODE = {
    "Full-Stack": "Next.js 14 App Router + Prisma + TypeScript + Expo/React Native. Server Actions para mutaciones web. 'use client' en Client Components. npx tsc --noEmit debe pasar. No generes Ruby ni Rails. Incluye tests (*.test.ts) para toda función pura nueva.",
    "Backend":    "Next.js API routes y Server Actions. Prisma para DB. TypeScript. Sin Rails. Escribe tests de integración para cada route/action nueva.",
    "Mobile":     "Expo/React Native. TypeScript. Respeta los tipos de mobile/lib/types.ts. No uses Flutter. Escribe tests unitarios para funciones puras en mobile/lib/.",
    "QA":         "Experto en calidad. Tu tarea es SOLO escribir tests, nunca el código de implementación. Cubre: unit tests en src/lib/*.test.ts y mobile/lib/*.test.ts (Vitest), integration tests para Server Actions y API routes, regression tests para cambios en código existente, E2E en tests/e2e/*.spec.ts (Playwright). Todos los tests deben ejecutar y pasar con `npx vitest run`.",
}


def _extract_token_usage(response: object, model_name: str) -> tuple:
    """Emite una línea estructurada con el uso de tokens y devuelve
    (input_tokens, output_tokens) para los atributos del span LLM (Req 6.4)."""
    try:
        inp = out = total = 0
        # LangChain >= 0.2: usage_metadata unificado
        um = getattr(response, "usage_metadata", None)
        if um:
            inp   = um.get("input_tokens", 0) or 0
            out   = um.get("output_tokens", 0) or 0
            total = um.get("total_tokens", inp + out) or (inp + out)
        else:
            # Fallback: response_metadata.token_usage (OpenAI-compat)
            rm = getattr(response, "response_metadata", {}) or {}
            tu = rm.get("token_usage") or rm.get("usage") or {}
            inp   = tu.get("prompt_tokens", 0) or 0
            out   = tu.get("completion_tokens", 0) or 0
            total = tu.get("total_tokens", inp + out) or (inp + out)
        if inp or out:
            _log(f"__TOKEN_USAGE__ {_json.dumps({'model': model_name, 'input': inp, 'output': out, 'total': total})}")
        return inp, out
    except Exception:
        return 0, 0


def _looks_like_gateway_error_page(content: str) -> bool:
    """True si `content` es una página de error HTML de un CDN/edge (Akamai u
    otro) en vez de la respuesta real del modelo — el proveedor upstream
    estaba caído/rechazando requests a nivel de gateway, no un error de la
    lógica del pipeline ni del código generado."""
    if not content:
        return False
    stripped = content.strip().lower()
    if stripped.startswith("<html") or stripped.startswith("<!doctype html"):
        return True
    return "an error occurred while processing your request" in stripped and "<body" in stripped


def _invoke_chain(primary, fallback_chain: list, messages: list) -> object:
    candidates = [primary] + fallback_chain
    last_exc = None
    for model in candidates:
        model_name = getattr(model, "model_name", str(model))
        for backoff in (0, 5, 15):
            if backoff:
                _log(f"[llm] {model_name}: reintentando tras error de gateway del proveedor (backoff {backoff}s)...")
                time.sleep(backoff)
            try:
                cycle = getattr(_thread_local, "current_cycle", 0)
                with fleet_tracing.llm_span(model_name, cycle) as span:
                    response = model.invoke(messages)
                    content = getattr(response, "content", "") or ""
                    if _looks_like_gateway_error_page(content):
                        last_exc = RuntimeError(
                            f"Error del proveedor del modelo: el gateway de {model_name} devolvió "
                            f"una página de error (no JSON) en vez de la respuesta del modelo: "
                            f"{content.strip()[:200]}"
                        )
                        logger.warning("Modelo %s devolvió página de error de gateway (reintento con backoff=%ds)", model_name, backoff)
                        continue
                    inp_tokens, out_tokens = _extract_token_usage(response, model_name)
                    fleet_tracing.set_llm_span_tokens(span, inp_tokens, out_tokens)
                    return response
            except Exception as exc:
                if _is_quota_error(exc) or "404" in str(exc):
                    logger.warning("Modelo %s no disponible -> siguiente fallback", model_name)
                    last_exc = exc
                    break  # no reintentar este modelo — pasar directo al siguiente de la cadena
                raise
        else:
            continue  # se agotaron los reintentos de error de gateway para este modelo — probar el siguiente
    raise RuntimeError(f"Todos los modelos fallaron. Ultimo error: {last_exc}") from last_exc


def _invoke_dev(messages: list) -> object:
    # Orden: Qwen Coder → Nemotron Ultra → Nemotron Super → Llama → MiniMax (último recurso)
    # MiniMax va al final: responde rápido pero no sigue el formato FILE_BEGIN/END
    return _invoke_chain(_qwen_dev, _OR_DEV_CHAIN + [_minimax_dev], messages)


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


def _make_jira_client():
    """Cliente Jira opcional: la Flota también resuelve requerimientos libres sin Jira."""
    if not (JIRA_URL and JIRA_USERNAME and JIRA_API_TOKEN):
        logger.info("Jira no configurado — modo requerimiento libre disponible (sin Jira).")
        return None
    try:
        return Jira(url=JIRA_URL, username=JIRA_USERNAME, password=JIRA_API_TOKEN, cloud=True)
    except Exception as e:  # noqa: BLE001
        logger.warning("No se pudo inicializar Jira (%s) — solo modo requerimiento libre.", e)
        return None


jira_client = _make_jira_client()

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


# Requerimiento 08: chequeo cruzado liviano entre un componente Angular
# X.html y su X.ts — detecta cuando el template referencia un método/símbolo
# que el agente nunca agregó al componente (regeneró solo uno de los dos
# archivos relacionados, o alucinó una estructura distinta que llama a un
# método inexistente). Es un chequeo por regex, no requiere compilar Angular.
_ANGULAR_TEMPLATE_EXPR_RE = _re.compile(
    r"\{\{(.*?)\}\}"                      # interpolación {{ ... }}
    r"|\*ng\w+\s*=\s*\"([^\"]*)\""        # directivas estructurales *ngIf/*ngFor
    r"|\[[\w.\-]+\]\s*=\s*\"([^\"]*)\"",  # property binding [x]="expr"
    _re.DOTALL,
)
_METHOD_CALL_RE = _re.compile(r"\b([a-zA-Z_]\w*)\s*\(")
_ANGULAR_TEMPLATE_BUILTINS = {
    "if", "for", "switch", "case", "let", "as",
    "true", "false", "null", "undefined", "this",
}


def _extract_template_method_calls(html_content: str) -> set:
    calls = set()
    for m in _ANGULAR_TEMPLATE_EXPR_RE.finditer(html_content):
        expr = next((g for g in m.groups() if g is not None), "")
        for call_m in _METHOD_CALL_RE.finditer(expr):
            name = call_m.group(1)
            if name not in _ANGULAR_TEMPLATE_BUILTINS:
                calls.add(name)
    return calls


def _method_exists_in_ts(name: str, ts_content: str) -> bool:
    return bool(_re.search(r"\b" + _re.escape(name) + r"\s*\(", ts_content))


# Requerimiento 11: el agente modificaba archivos de implementación que el
# requerimiento nunca autorizó tocar — ej. "ajusta el TEST, no la
# implementación" para X.test.ts, y el agente reescribía X.ts igual; o el
# requerimiento solo mencionaba X.test.ts y el agente tocó X.ts sin que nadie
# lo pidiera. Frases que indican que el requerimiento SÍ contempla
# explícitamente una excepción condicional para tocar la implementación
# (ej. "...salvo que confirmes que la implementación tiene el bug real") —
# en ese caso no se bloquea automáticamente, solo se deja visible en el log
# para que el reviewer le preste atención extra.
_SCOPE_EXCEPTION_MARKERS = ("salvo que", "excepto si", "a menos que", "salvo si", "a no ser que")


def _find_implicitly_protected_impl_files(criteria: str) -> Dict[str, str]:
    """Deriva, del texto libre del requerimiento, qué archivos de
    implementación NO están autorizados a modificarse: el companion de
    implementación (`X.ts`) de un archivo de test (`X.test.ts`/`X.spec.ts`)
    mencionado en el texto, cuando ese companion nunca se menciona por su
    cuenta en ningún otro lugar del requerimiento.

    Devuelve {ruta_implementacion: "hard" | "soft"}. "hard" = no hay ningún
    lenguaje de excepción condicional en el texto, se rechaza el ciclo si se
    toca. "soft" = sí hay lenguaje de excepción (ver _SCOPE_EXCEPTION_MARKERS),
    no se bloquea automáticamente, solo se registra para el reviewer.
    """
    if not criteria:
        return {}
    protected: Dict[str, str] = {}
    criteria_lower = criteria.lower()
    has_exception_language = any(marker in criteria_lower for marker in _SCOPE_EXCEPTION_MARKERS)
    test_paths = set(_re.findall(r"[\w/\-\.]+\.(?:test|spec)\.tsx?", criteria))
    for test_path in test_paths:
        impl_path = _re.sub(r"\.(test|spec)\.", ".", test_path, count=1)
        # El companion cuenta como "mencionado" solo si aparece en el texto
        # en una posición DISTINTA a la del propio test_path (para no
        # confundir "X.test.ts" consigo mismo si compartiera substring).
        other_mentions = [
            m.start() for m in _re.finditer(_re.escape(impl_path), criteria)
            if criteria[m.start(): m.start() + len(test_path)] != test_path
        ]
        if other_mentions:
            continue  # el requerimiento sí menciona la implementación aparte
        protected[impl_path] = "soft" if has_exception_language else "hard"
    return protected


def _find_allow_rewrite_files(criteria: str) -> set:
    """Extrae las rutas marcadas con `ALLOW_REWRITE: <ruta>` en el texto del
    requerimiento (requerimiento 13): opt-out explícito y determinístico de
    las guardas de tamaño (truncamiento + reescritura excesiva) para
    simplificaciones masivas intencionales — ej. migrar ~150 líneas de lógica
    inline a un adaptador delgado de ~20 líneas que llama a un servicio ya
    extraído. Quien redacta el requerimiento (que ya revisó el plan) asume la
    responsabilidad; la marca queda auditable en el log.

    Solo desactiva las guardas basadas en tamaño; el resto (alcance del
    requerimiento, chequeo cruzado Angular) sigue vigente."""
    if not criteria:
        return set()
    return {m.strip() for m in _re.findall(r"ALLOW_REWRITE:\s*([^\n\r]+)", criteria)}


def _apply_workspace_changes(workspace: str, llm_response: str, criteria: str = "") -> tuple:
    """Extrae bloques ===FILE_BEGIN/END=== de la respuesta del LLM y los escribe al disco.

    Devuelve (applied, rejected). Un archivo se rechaza (no se escribe) si:
    1. Ya existe, es "grande" (>500 líneas o >20KB) y el contenido propuesto
       perdió >30% de líneas/bytes — señal de truncamiento (requerimiento 06).
    2. Ya existe, tiene >100 líneas, y el contenido propuesto cambió >30% de
       las líneas aunque el tamaño no se haya achicado — señal de reescritura
       completa no solicitada, el patrón más sutil del requerimiento 08 (el
       modelo "alucina" una versión distinta del archivo en vez de aplicar el
       cambio puntual pedido; el guard de tamaño de arriba no lo detecta
       porque el archivo resultante tiene un tamaño similar). Umbral bajado
       de 80% a 30% por el requerimiento 12 (un cambio real de ~65% pasó sin
       detectarse con el umbral anterior).
    3. Es un `.html` de Angular que referencia (vía `{{ }}`/`*ngIf`/binding)
       un método que no existe en su `.ts` correspondiente (ni en el nuevo
       contenido de este mismo ciclo, ni en el que ya está en disco) — señal
       de que el agente tocó un archivo del par sin coordinar el otro.
    4. Es un archivo de implementación cuyo companion de test SÍ fue
       mencionado en `criteria` pero el archivo de implementación en sí
       nunca se mencionó (ver requerimiento 11) — protección "hard". Si el
       requerimiento tiene lenguaje de excepción condicional (ej. "salvo que
       confirmes..."), se permite pero se registra como protección "soft"
       (solo warning en el log, no se rechaza).

    Las guardas 1 y 2 (basadas en tamaño) se desactivan por archivo con la
    marca `ALLOW_REWRITE: <ruta>` en `criteria` (requerimiento 13): opt-out
    explícito para simplificaciones masivas intencionales, sin afectar a los
    demás archivos del ciclo.
    """
    applied = []
    rejected = []
    protected_impl_files = _find_implicitly_protected_impl_files(criteria)
    allow_rewrite_files = _find_allow_rewrite_files(criteria)
    # [^\n\r=]+ evita que el path capture newlines o el === de cierre
    # [ \t]*\r?\n? hace el salto de linea despues de === opcional
    pattern = r"===FILE_BEGIN:\s*([^\n\r=]+?)===[ \t]*\r?\n?(.*?)===FILE_END==="
    matches = _re.findall(pattern, llm_response, _re.DOTALL)

    # Primera pasada: normalizar rutas/contenido y aplicar las guardas de
    # tamaño/reescritura. `candidates` acumula lo que pasó estas guardas.
    candidates: Dict[str, str] = {}
    for rel_path, content in matches:
        rel_path = rel_path.strip()
        # Si el modelo uso \n literal en lugar de newlines reales, desescapar
        if "\\n" in content and "\n" not in content:
            content = content.replace("\\n", "\n").replace("\\t", "\t").replace("\\r", "")
        # Si el LLM generó una ruta absoluta del host (ej. /Users/moyarzun/proyecto/foo.rs),
        # os.path.join ignoraría el workspace y el contenedor no tiene ese mount → EPERM.
        # Normalizamos: si la ruta es absoluta, intentamos hacerla relativa al workspace;
        # si no tiene el prefijo del workspace, simplemente quitamos la barra inicial.
        if os.path.isabs(rel_path):
            workspace_norm = workspace.rstrip("/")
            if rel_path.startswith(workspace_norm + "/"):
                rel_path = rel_path[len(workspace_norm) + 1:]
            else:
                rel_path = rel_path.lstrip("/")
        full_path = os.path.join(workspace, rel_path)

        protection = protected_impl_files.get(rel_path)
        if protection == "hard":
            reason = (
                f"{rel_path}: el requerimiento no autorizó modificar este archivo de "
                f"implementación (solo mencionaba su test) — rechazado por guarda de "
                f"alcance no autorizado"
            )
            rejected.append(reason)
            logger.warning("Archivo %s rechazado: fuera del alcance autorizado por el requerimiento", full_path)
            continue
        elif protection == "soft":
            logger.warning(
                "Archivo %s modificado pese a que el requerimiento solo mencionaba su "
                "test explícitamente — permitido por lenguaje de excepción condicional "
                "en el requerimiento, pero requiere atención extra del reviewer",
                full_path,
            )

        allow_rewrite = rel_path in allow_rewrite_files
        if allow_rewrite:
            logger.info(
                "Archivo %s: guardas de tamaño (truncamiento/reescritura) desactivadas "
                "por marca ALLOW_REWRITE en el requerimiento", rel_path,
            )

        if os.path.exists(full_path) and not allow_rewrite:
            try:
                with open(full_path, "r", errors="ignore") as fh:
                    original_text = fh.read()
            except OSError:
                original_text = ""
            original_lines_list = original_text.splitlines()
            original_lines = len(original_lines_list)
            original_size = len(original_text.encode("utf-8", errors="ignore"))
            new_lines_list = content.splitlines()
            new_lines = len(new_lines_list)
            new_size = len(content.encode("utf-8", errors="ignore"))

            is_large = original_lines > 500 or original_size > 20_000
            shrank_a_lot = (
                original_lines > 0 and new_lines < original_lines * 0.7
            ) or (
                original_size > 0 and new_size < original_size * 0.7
            )
            if is_large and shrank_a_lot:
                reason = (
                    f"{rel_path}: original {original_lines} líneas/{original_size}B "
                    f"→ propuesto {new_lines} líneas/{new_size}B "
                    f"(pérdida >30%, rechazado por guarda anti-truncamiento)"
                )
                rejected.append(reason)
                logger.warning(
                    "Archivo %s rechazado: encogió de %d a %d líneas (posible "
                    "truncamiento/reescritura no solicitada del LLM)",
                    full_path, original_lines, new_lines,
                )
                continue  # no escribir este archivo

            if original_lines > 100:
                similarity = difflib.SequenceMatcher(None, original_lines_list, new_lines_list).ratio()
                # Umbral alineado con el resto de las guardas (>30% cambiado, ver
                # requerimiento 12): un primer umbral de >80% dejó pasar sin
                # detectar una reescritura real de ~65% en payment-service.ts que
                # introdujo regresiones (se eliminó un límite de paginación,
                # includes de Prisma, el tipo Zod compartido, y el helper de
                # autorización reusable) — el rechazo conservador cuesta un ciclo
                # extra de iteración, pero es preferible a aprobar eso en silencio.
                if similarity < 0.70:
                    changed_pct = round((1 - similarity) * 100)
                    reason = (
                        f"{rel_path}: ~{changed_pct}% del archivo cambiado "
                        f"(rechazado por guarda de reescritura excesiva — el cambio "
                        f"pedido no debería requerir reescribir el archivo casi entero)"
                    )
                    rejected.append(reason)
                    logger.warning(
                        "Archivo %s rechazado: reescritura excesiva (similaridad %.0f%%)",
                        full_path, similarity * 100,
                    )
                    continue

        candidates[rel_path] = content

    # Segunda pasada: chequeo cruzado Angular .ts/.html sobre lo que sobrevivió
    # a las guardas de arriba.
    for rel_path in list(candidates.keys()):
        if not rel_path.endswith(".html"):
            continue
        calls = _extract_template_method_calls(candidates[rel_path])
        if not calls:
            continue
        ts_rel_path = rel_path[: -len(".html")] + ".ts"
        ts_content = candidates.get(ts_rel_path)
        if ts_content is None:
            ts_full_path = os.path.join(workspace, ts_rel_path)
            if not os.path.exists(ts_full_path):
                continue  # no hay .ts correspondiente — no es un componente Angular típico
            try:
                with open(ts_full_path, "r", errors="ignore") as fh:
                    ts_content = fh.read()
            except OSError:
                ts_content = ""
        missing = [name for name in sorted(calls) if not _method_exists_in_ts(name, ts_content)]
        if missing:
            reason = (
                f"{rel_path}: el template referencia {', '.join(missing)} pero no "
                f"existe en {ts_rel_path} (rechazado por chequeo cruzado .ts/.html)"
            )
            rejected.append(reason)
            logger.warning(
                "Archivo %s rechazado: referencia símbolos inexistentes en %s: %s",
                rel_path, ts_rel_path, missing,
            )
            del candidates[rel_path]

    # Tercera pasada: escribir al disco solo lo que sobrevivió a todas las guardas.
    for rel_path, content in candidates.items():
        full_path = os.path.join(workspace, rel_path)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        # Eliminar antes de escribir para evitar deadlock VirtioFS (Errno 35):
        # los archivos existentes en volúmenes macOS heredan xattrs que bloquean escritura.
        if os.path.exists(full_path):
            try:
                os.unlink(full_path)
            except OSError:
                pass
        with open(full_path, "w") as fh:
            fh.write(content)
        applied.append(rel_path)
        logger.info("Archivo escrito: %s", full_path)
    return applied, rejected


# ===========================================================================
# 2b. Helpers de stack, git (GitFlow) y validación determinista
# ===========================================================================
def _run(cmd: list, cwd: str, timeout: int = 120) -> tuple:
    """Ejecuta un comando y devuelve (returncode, salida_combinada)."""
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, f"TIMEOUT tras {timeout}s: {' '.join(cmd)}"
    except FileNotFoundError:
        return 127, f"comando no encontrado: {cmd[0]}"
    except Exception as e:  # noqa: BLE001
        return 1, f"error ejecutando {cmd}: {e}"


def _tool_available(tool: str) -> bool:
    rc, _ = _run([tool, "--version"], cwd="/", timeout=15)
    return rc == 0


def _detect_stack(workspace: str) -> str:
    """rails | flutter | node | generic, según los archivos marcador del repo."""
    has = lambda f: os.path.exists(os.path.join(workspace, f))  # noqa: E731
    if has("Gemfile") and (has("config/application.rb") or has("bin/rails")):
        return "rails"
    if has("pubspec.yaml"):
        return "flutter"
    if has("package.json"):
        return "node"
    return "generic"


def _git(args: list, workspace: str, timeout: int = 60) -> tuple:
    return _run(["git", *args], cwd=workspace, timeout=timeout)


def _is_git_repo(workspace: str) -> bool:
    rc, out = _git(["rev-parse", "--is-inside-work-tree"], workspace)
    return rc == 0 and "true" in out


def _slugify(text: str, maxlen: int = 40) -> str:
    s = _re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return (s[:maxlen]).strip("-") or "cambios"


def _rails_grounding(workspace: str, criteria: str, max_bytes: int = 6000) -> str:
    """Triangulación spec↔factory↔esquema: el contexto que evita modelos inventados.

    Adjunta db/schema.rb (recortado), y los spec/factory cuyo nombre aparezca en los
    criterios del ticket (heurística por palabra clave del título/descripción).
    """
    parts = []
    # Esquema de control
    schema = os.path.join(workspace, "db", "schema.rb")
    if os.path.exists(schema):
        with open(schema, "r", errors="replace") as fh:
            parts.append(f"===SCHEMA: db/schema.rb (recortado)===\n{fh.read(max_bytes)}\n===END===")
    # Palabras candidatas (nombres de modelo/recurso) extraídas de los criterios
    words = {w.lower() for w in _re.findall(r"[A-Za-z_][A-Za-z0-9_]{3,}", criteria or "")}
    def _attach_matching(subdir: str, label: str):
        base = os.path.join(workspace, subdir)
        if not os.path.isdir(base):
            return
        for root, _d, files in os.walk(base):
            for fn in files:
                if not fn.endswith(".rb"):
                    continue
                stem = fn.replace("_spec.rb", "").replace(".rb", "")
                if stem in words or stem.rstrip("s") in words:
                    fp = os.path.join(root, fn)
                    try:
                        with open(fp, "r", errors="replace") as fh:
                            rel = os.path.relpath(fp, workspace)
                            parts.append(f"==={label}: {rel}===\n{fh.read(max_bytes)}\n===END===")
                    except Exception:  # noqa: BLE001
                        pass
    _attach_matching("spec/models", "SPEC")
    _attach_matching("spec/factories", "FACTORY")
    _attach_matching("db/tenant_migrate", "MIGRATION")
    _attach_matching("db/migrate", "MIGRATION")
    if not parts:
        return ""
    return ("# GROUNDING (fuente de verdad — el código nuevo DEBE coincidir con esto):\n"
            + "\n".join(parts))


def _extract_failing_tests(vitest_output: str) -> set:
    """Extrae identificadores únicos de tests que fallaron de la salida verbose
    de `vitest run` (líneas 'FAIL <archivo> > describe > test'). Se usa para
    comparar qué falla ANTES vs DESPUÉS de que dynamic_developer edite código
    (ver requerimiento 10: un ticket acotado no debe rechazarse por fallos
    preexistentes no relacionados en otra parte del repo)."""
    failing = set()
    for line in vitest_output.splitlines():
        line = line.strip()
        if line.startswith("FAIL"):
            ident = line[len("FAIL"):].strip()
            if ident:
                failing.add(ident)
    return failing


def _run_vitest_baseline(workspace: str, stack: str) -> Optional[set]:
    """Corre `vitest run` UNA vez, antes de que dynamic_developer toque nada,
    para saber qué tests ya fallaban de antemano. Devuelve None si vitest no
    aplica a este proyecto (no hay vitest.config, o no es stack node) — en
    ese caso _validate_workspace no puede distinguir preexistente de nuevo y
    cae al comportamiento estricto anterior (cualquier fallo bloquea)."""
    if stack != "node" or not _tool_available("npx"):
        return None
    has_vitest_config = os.path.exists(os.path.join(workspace, "vitest.config.ts")) or \
                        os.path.exists(os.path.join(workspace, "vitest.config.js"))
    if not has_vitest_config:
        return None
    rc, out = _run(
        ["node", "--max-old-space-size=2048", "./node_modules/.bin/vitest",
         "run", "--reporter=verbose"],
        cwd=workspace,
        timeout=int(os.getenv("FLEET_VITEST_TIMEOUT", "180")),
    )
    if "Cannot find module" in out and "vitest" in out:
        return None  # vitest no instalado — _validate_workspace ya avisa de esto por su cuenta
    if rc == 0:
        return set()
    return _extract_failing_tests(out)


def _validate_workspace(
    workspace: str, applied_files: list, stack: str = "generic",
    baseline_failing_tests: Optional[set] = None,
) -> tuple:
    """Validación DETERMINISTA del código generado. Devuelve (passed, reporte).

    Niveles (best-effort según herramientas disponibles en el contenedor):
      1. Sintaxis Ruby (`ruby -c`) sobre cada .rb tocado — siempre que haya ruby.
      2. Comando de validación del proyecto si existe (FLEET_VALIDATE_CMD,
         `bin/fleet-validate` o `make validate`) — aquí el proyecto corre SUS tests
         (rspec, flutter analyze, etc.) en un entorno con su toolchain.
    Si NINGÚN nivel pudo correr, passed=False con aviso (no aprobar a ciegas).
    """
    report = []
    ran_any = False
    ok = True

    # ── Nivel 1a: sintaxis Ruby (solo proyectos Rails) ─────────────────────
    rb_files = [f for f in applied_files if f.endswith(".rb")]
    if rb_files and stack == "rails" and _tool_available("ruby"):
        ran_any = True
        syntax_errors = []
        for rel in rb_files:
            rc, out = _run(["ruby", "-c", rel], cwd=workspace, timeout=30)
            if rc != 0:
                syntax_errors.append(f"  ✗ {rel}: {out.strip().splitlines()[-1] if out.strip() else 'syntax error'}")
        if syntax_errors:
            ok = False
            report.append("SINTAXIS RUBY (ruby -c) — ERRORES:\n" + "\n".join(syntax_errors))
        else:
            report.append(f"SINTAXIS RUBY: ✓ {len(rb_files)} archivos .rb parsean correctamente.")

    # ── Nivel 1b: TypeScript check (proyectos node) ────────────────────────
    ts_files = [f for f in applied_files if f.endswith((".ts", ".tsx"))]
    if ts_files and stack == "node":
        tsc_available = _tool_available("npx")
        if tsc_available:
            ran_any = True
            # Aumentar memoria para proyectos grandes; skipLibCheck para velocidad
            rc, out = _run(
                ["node", "--max-old-space-size=2048", "./node_modules/.bin/tsc",
                 "--noEmit", "--skipLibCheck", "--incremental", "false"],
                cwd=workspace, timeout=180,
            )
            oom = "Last few GCs" in out or "heap out of memory" in out.lower() or "JavaScript heap" in out
            if oom:
                # OOM no es fallo del código generado — avisar y continuar
                report.append("TYPESCRIPT (tsc --noEmit): ⚠ OOM en el contenedor — check omitido")
            elif rc == 0:
                report.append("TYPESCRIPT (tsc --noEmit): ✓ sin errores de tipos")
            else:
                # Filtrar errores en archivos que la flota NO generó (pre-existentes).
                # Un error en un archivo ajeno no debe bloquear el trabajo de la flota.
                applied_set = set(applied_files)
                error_lines = out.strip().splitlines()
                # Patrón: "ruta/archivo.ts(linea,col): error TSxxxx: ..."
                fleet_errors = []
                preexisting_errors = []
                for line in error_lines:
                    m = _re.match(r'^([^(]+)\(\d+,\d+\): error', line)
                    if m:
                        file_path = m.group(1).replace("\\", "/").lstrip("./")
                        if file_path in applied_set:
                            fleet_errors.append(line)
                        else:
                            preexisting_errors.append(line)
                    else:
                        # Líneas de continuación o resumen: adjuntar al último bucket
                        if fleet_errors:
                            fleet_errors.append(line)
                        elif preexisting_errors:
                            preexisting_errors.append(line)

                # Errores de módulos mobile (react-native, expo) en archivos fleet son
                # falsos positivos: el tsconfig web no ve esas deps. No bloquear.
                _MOBILE_MODULES = (
                    "'react-native'", "'@expo/", "'expo-", "'react-native-",
                    "'@react-native", "@react-navigation",
                )
                real_fleet_errors = [
                    l for l in fleet_errors
                    if not any(m in l for m in _MOBILE_MODULES)
                ]
                env_mobile_errors = [
                    l for l in fleet_errors if l not in real_fleet_errors
                ]

                if real_fleet_errors:
                    ok = False
                    tail = "\n".join(real_fleet_errors[-30:])
                    report.append(f"TYPESCRIPT (tsc --noEmit): ✗\n{tail}")
                else:
                    ignored = len(preexisting_errors) + len(env_mobile_errors)
                    report.append(
                        f"TYPESCRIPT (tsc --noEmit): ✓ sin errores en archivos generados "
                        f"(ignorados: {ignored} línea(s) pre-existentes o módulos mobile)"
                    )

    # ── Nivel 1c: Vitest unit/integration tests (proyectos node) ──────────────
    # Corre los tests de Vitest cuando:
    #   - stack es node y npx disponible
    #   - hay archivos *.test.ts/.spec.ts entre los archivos generados, O hay vitest.config.ts
    # El gate solo bloquea si los tests FALLAN (no si vitest no está instalado).
    if stack == "node" and ok and _tool_available("npx"):
        has_test_files = any(
            ".test." in f or (f.endswith(".spec.ts") and "e2e" not in f)
            for f in applied_files
        )
        has_vitest_config = os.path.exists(os.path.join(workspace, "vitest.config.ts")) or \
                            os.path.exists(os.path.join(workspace, "vitest.config.js"))
        if has_test_files or has_vitest_config:
            ran_any = True
            rc, out = _run(
                ["node", "--max-old-space-size=2048", "./node_modules/.bin/vitest",
                 "run", "--reporter=verbose"],
                cwd=workspace,
                timeout=int(os.getenv("FLEET_VITEST_TIMEOUT", "180")),
            )
            tail = "\n".join(out.strip().splitlines()[-40:])
            if "Cannot find module" in out and "vitest" in out:
                report.append("VITEST: ⚠ vitest no encontrado en node_modules — instala con npm install")
            elif rc == 0:
                # Extraer resumen compacto de tests pasados
                summary_match = _re.search(r'Test Files.*\n?.*Tests\s+\d+', out)
                summary = summary_match.group(0).strip() if summary_match else f"exit 0"
                report.append(f"VITEST (npx vitest run): ✓ {summary}\n{tail[-800:]}")
            elif baseline_failing_tests is not None:
                # Requerimiento 10: aprobar por alcance del ticket, no exigir que
                # TODA la suite esté verde. Solo bloquear si aparecen fallos
                # NUEVOS que no estaban ya presentes antes de este despacho.
                current_failing = _extract_failing_tests(out)
                new_failures = current_failing - baseline_failing_tests
                preexisting_still_failing = current_failing & baseline_failing_tests
                if new_failures:
                    ok = False
                    report.append(
                        f"VITEST (npx vitest run): ✗ {len(new_failures)} fallo(s) NUEVO(S) "
                        f"introducido(s) por este cambio (no relacionado con fallos "
                        f"preexistentes):\n" + "\n".join(sorted(new_failures)[:20])
                    )
                else:
                    report.append(
                        f"VITEST (npx vitest run): ✓ sin fallos nuevos "
                        f"({len(preexisting_still_failing)} fallo(s) preexistente(s) "
                        f"no relacionados con este ticket, ya presentes antes del despacho, "
                        f"ignorados)\n{tail[-800:]}"
                    )
            else:
                ok = False
                report.append(f"VITEST (npx vitest run): ✗ exit {rc}\n{tail}")
        else:
            report.append(
                "VITEST: ⚠ no hay archivos *.test.ts ni vitest.config.ts detectados. "
                "OBLIGATORIO: todo cambio de código debe incluir sus tests unitarios "
                "(src/lib/*.test.ts o mobile/lib/*.test.ts). Agrega tests antes de continuar."
            )
            ok = False  # Forzar ciclo de corrección si no hay tests

    # ── Nivel 2: comando de validación del proyecto (tests reales) ───────────
    validate_cmd = os.getenv("FLEET_VALIDATE_CMD")
    candidates = []
    if validate_cmd:
        candidates.append(validate_cmd.split())
    if os.path.exists(os.path.join(workspace, "bin", "fleet-validate")):
        candidates.append(["bin/fleet-validate"])
    if os.path.exists(os.path.join(workspace, "Makefile")):
        rc, out = _run(["grep", "-q", "^validate:", "Makefile"], cwd=workspace, timeout=10)
        if rc == 0:
            candidates.append(["make", "validate"])
    if candidates:
        ran_any = True
        cmd = candidates[0]
        rc, out = _run(cmd, cwd=workspace, timeout=int(os.getenv("FLEET_VALIDATE_TIMEOUT", "600")))
        tail = "\n".join(out.strip().splitlines()[-40:])
        if rc == 0:
            report.append(f"VALIDACIÓN PROYECTO (`{' '.join(cmd)}`): ✓ exit 0\n{tail}")
        else:
            ok = False
            report.append(f"VALIDACIÓN PROYECTO (`{' '.join(cmd)}`): ✗ exit {rc}\n{tail}")

    if not ran_any:
        return False, ("No se pudo correr ninguna validación determinista (sin ruby ni "
                       "comando de validación del proyecto). Define FLEET_VALIDATE_CMD o "
                       "agrega bin/fleet-validate / target `validate` en el Makefile del "
                       "workspace para que la Flota ejecute los tests reales.")
    return ok, "\n\n".join(report)


# ===========================================================================
# 3. Estado del grafo y esquemas Pydantic
# ===========================================================================
class FleetState(TypedDict):
    messages:            Annotated[List[BaseMessage], add_messages]
    ticket_id:           str
    requirement:         str         # requerimiento libre (sin Jira); si está, ignora ticket_id
    workspace_path:      str
    acceptance_criteria: str
    required_agents:     List[str]
    current_code_diff:   Dict[str, str]
    applied_files:       List[str]   # rutas relativas escritas al disco en todos los ciclos
    rejected_files:      List[str]   # archivos rechazados por la guarda anti-truncamiento (no escritos)
    reviewer_feedback:   str
    is_approved:         bool
    loop_iterations:     int
    aborted:             bool        # True si git_setup abortó (sin repo git / árbol sucio)
    # ── GitFlow + validación + planificación ──
    stack:               str         # rails | flutter | node | generic
    base_branch:         str         # rama base (main/develop) sobre la que se ramifica
    work_branch:         str         # rama de trabajo fleet/<ticket>-<slug>
    subtasks:            List[str]   # descomposición del ticket (planner)
    validation_report:   str         # salida determinista (ruby -c / tests)
    validation_passed:   bool        # gate determinista
    validation_baseline_failing_tests: Optional[List[str]]  # tests que ya fallaban ANTES del despacho (None si no aplica/no se pudo capturar)
    pr_url:              str         # URL del PR si se abrió
    existing_files:      Dict[str, str]  # contenido de archivos existentes antes de modificar
    regression_errors:   List[str]       # elementos eliminados detectados por regression_guard
    # ── Staging tester ──
    staging_url:         str         # URL del deploy de staging (Vercel preview o STAGING_BASE_URL)
    staging_passed:      bool        # True si los smoke/E2E de staging pasaron (o si staging no está configurado)
    staging_report:      str         # salida del staging_tester_node
    # ── Límite de recursión (spec langgraph-hardening, Req 7.3) ──
    remaining_steps:     RemainingSteps  # super-steps restantes antes del recursion_limit (managed value)


class ReviewerDecision(BaseModel):
    is_approved:         bool = Field(description="True si la solucion cumple los criterios de aceptacion.")
    corrective_feedback: str  = Field(description="Retroalimentacion correctiva si falla; resumen si aprueba.")


# ===========================================================================
# 3b. Helpers de análisis de regresión (codebase_reader / regression_guard)
# ===========================================================================

# Archivos clave a leer siempre según el stack (independientemente de lo que diga el planner)
_KEY_FILES_BY_STACK: Dict[str, List[str]] = {
    "node": [
        "prisma/schema.prisma",
        "package.json",
        "vitest.config.ts",
        "vitest.config.js",
        "src/lib/sms.ts",
        "src/lib/auth.ts",
        "src/server/auth/context.ts",
        "src/server/http.ts",
        "src/lib/mobile-api.ts",
    ],
    "rails": [
        "db/schema.rb",
        "Gemfile",
    ],
    "flutter": [
        "pubspec.yaml",
    ],
}


def _check_prisma_regression(old: str, new: str) -> List[str]:
    """Detecta modelos o campos Prisma eliminados en la versión generada."""
    issues = []

    old_models = set(_re.findall(r'^model\s+(\w+)\s*\{', old, _re.MULTILINE))
    new_models = set(_re.findall(r'^model\s+(\w+)\s*\{', new, _re.MULTILINE))
    removed_models = old_models - new_models
    if removed_models:
        issues.append(f"modelos eliminados del schema: {sorted(removed_models)}")

    for model in (old_models & new_models):
        old_block = _re.search(rf'^model\s+{model}\s*\{{([^}}]+)\}}', old, _re.MULTILINE | _re.DOTALL)
        new_block = _re.search(rf'^model\s+{model}\s*\{{([^}}]+)\}}', new, _re.MULTILINE | _re.DOTALL)
        if not old_block or not new_block:
            continue
        old_fields = set(_re.findall(r'^\s{1,4}(\w+)\s+\w', old_block.group(1), _re.MULTILINE))
        new_fields = set(_re.findall(r'^\s{1,4}(\w+)\s+\w', new_block.group(1), _re.MULTILINE))
        removed = old_fields - new_fields - {'@@', '//'}
        if removed:
            issues.append(f"modelo {model}: campos eliminados: {sorted(removed)}")

    return issues


def _check_ts_exports_regression(old: str, new: str) -> List[str]:
    """Detecta exports de TypeScript/JS eliminados en la versión generada."""
    pat = r'^export\s+(?:async\s+)?(?:function|class|const|type|interface|enum)\s+(\w+)'
    old_exports = set(_re.findall(pat, old, _re.MULTILINE))
    new_exports = set(_re.findall(pat, new, _re.MULTILINE))
    removed = old_exports - new_exports
    if removed:
        return [f"exports eliminados: {sorted(removed)}"]
    return []


# ===========================================================================
# 4. Nodos del grafo
# ===========================================================================
def fetch_and_plan_node(state: FleetState) -> dict:
    ticket_id   = state.get("ticket_id", "")
    requirement = (state.get("requirement") or "").strip()
    preset_agents = state.get("required_agents") or []

    if requirement:
        # ── Modo requerimiento libre: cualquier proyecto, sin Jira ──────────────
        summary     = requirement.split("\n", 1)[0][:120]
        description = requirement
        _log(f"[context_ingestion] Modo requerimiento libre: {summary}")
        required_agents = preset_agents or ["Full-Stack"]
    elif jira_client is not None:
        # ── Modo Jira (compatibilidad) ──────────────────────────────────────────
        _log(f"[context_ingestion] Leyendo ticket {ticket_id} desde Jira...")
        issue_data  = jira_client.issue(ticket_id)
        summary     = issue_data["fields"]["summary"]
        description = issue_data["fields"]["description"] or ""
        labels      = issue_data["fields"].get("labels", [])
        _log(f"[context_ingestion] Ticket: {summary}")
        required_agents = [lbl.split(":")[1] for lbl in labels if lbl.startswith("agent:")]
        required_agents = required_agents or preset_agents or ["Full-Stack"]
    else:
        raise RuntimeError(
            "Sin 'requirement' ni Jira configurado. Provee un requerimiento libre "
            "o configura JIRA_URL/JIRA_USER/JIRA_API_TOKEN."
        )

    _log(f"[context_ingestion] Agentes asignados: {required_agents}")
    desc_preview = (description[:120] + "...") if len(description) > 120 else description
    _log(f"[context_ingestion] Criterios: {desc_preview}")

    context_string = f"TITULO: {summary}\n\nCRITERIOS DE ACEPTACION:\n{description}"
    stack = _detect_stack(state["workspace_path"])
    _log(f"[context_ingestion] Stack detectado: {stack}")
    return {
        "acceptance_criteria": context_string,
        "required_agents":     required_agents,
        "stack":               stack,
        "is_approved":         False,
        "validation_passed":   False,
        "loop_iterations":     0,
        "current_code_diff":   {},
        "applied_files":       [],
        "messages":            [AIMessage(content=f"Agentes requeridos: {required_agents} · stack={stack}", name="Planner")],
    }


def _abort(reason: str) -> dict:
    """Corta la ejecución del grafo en git_setup con un motivo claro.

    Setea 'aborted' (usado por la arista condicional post-git_setup para saltar
    directo a END) y también 'is_approved'/'reviewer_feedback' para que el
    resultado final del job (fleet_api._run_fleet_worker) muestre el motivo
    real en vez de un rechazo genérico.
    """
    _log(f"[git_setup] {reason}")
    return {
        "aborted":            True,
        "base_branch":        "",
        "work_branch":        "",
        "is_approved":        False,
        "validation_passed":  False,
        "reviewer_feedback":  reason,
        "messages":           [AIMessage(content=reason, name="GitOps")],
    }


def git_setup_node(state: FleetState) -> dict:
    """GitFlow: crea un worktree aislado por ticket. NUNCA se trabaja en main
    ni se comparte working tree entre tareas concurrentes.

    ABORTA la ejecución completa (no degrada ni continúa) si:
      - el workspace no es un repositorio git (o git no está disponible), o
      - el árbol de trabajo tiene cambios sin commitear.

    Usa `git worktree add` en vez de `git checkout -B`: cada tarea recibe su
    propio directorio físico checkouteado a su propia rama, compartiendo el
    mismo `.git` — así múltiples tareas pueden correr en paralelo sobre el
    mismo repo sin pisarse el working tree entre sí.
    """
    workspace = state["workspace_path"]
    ticket    = state["ticket_id"]

    if not _tool_available("git") or not _is_git_repo(workspace):
        return _abort(
            "ABORTADO: el workspace no es un repositorio git (o git no está "
            "disponible). La Flota nunca opera sobre directorios sin control "
            "de versiones — no hay red de seguridad para revertir cambios. "
            "Inicializa el repo (git init + primer commit) ANTES de invocar "
            "la Flota."
        )

    rc, out = _git(["status", "--porcelain"], workspace)
    if rc == 0 and out.strip():
        return _abort(
            "ABORTADO: el árbol de trabajo tiene cambios sin commitear. La "
            "Flota ya no hace `git stash` (ocultaba cambios del usuario sin "
            "restaurarlos). Commitea o descarta tus cambios antes de invocar "
            "la Flota.\n\ngit status --porcelain:\n" + out.strip()[:500]
        )

    # Rama base: la que esté actualmente checked out en el workspace (así el
    # trabajo despachado se apila sobre la rama activa del usuario, no
    # siempre sobre develop/main). Si HEAD está detached, cae al
    # comportamiento anterior (develop si existe, si no la rama por defecto).
    rc, out = _git(["symbolic-ref", "--short", "HEAD"], workspace)
    if rc == 0 and out.strip():
        base = out.strip()
    else:
        rc, out = _git(["rev-parse", "--verify", "develop"], workspace)
        if rc == 0:
            base = "develop"
        else:
            rc, out = _git(["symbolic-ref", "refs/remotes/origin/HEAD"], workspace)
            base = out.strip().split("/")[-1] if rc == 0 and out.strip() else "main"

    slug   = _slugify(state.get("acceptance_criteria", "").split("\n")[0].replace("TITULO:", ""))
    branch = f"fleet/{ticket}-{slug}"

    # Worktree aislado como sibling del repo (NUNCA dentro del propio workspace
    # montado), para no ensuciar el árbol que el usuario ve y permitir que
    # varias tareas corran en paralelo sin compartir working tree.
    repo_name     = os.path.basename(workspace.rstrip("/")) or "repo"
    worktree_root = os.path.join(os.path.dirname(workspace.rstrip("/")), ".fleet-worktrees")
    worktree_path = os.path.join(worktree_root, f"{repo_name}-{ticket}-{slug}")
    os.makedirs(worktree_root, exist_ok=True)

    # Si quedó un worktree de un intento anterior con el mismo nombre, limpiarlo.
    if os.path.exists(worktree_path):
        _git(["worktree", "remove", "--force", worktree_path], workspace)
        _git(["worktree", "prune"], workspace)
        shutil.rmtree(worktree_path, ignore_errors=True)

    # Traer la base más reciente sin tocar el checkout compartido: el worktree
    # nuevo parte de origin/<base> si el fetch funcionó (repo con remoto), o de
    # la base local como fallback (repo sin remoto / fetch falló).
    fetch_rc, _ = _git(["fetch", "origin", base], workspace, timeout=120)
    base_ref = f"origin/{base}" if fetch_rc == 0 else base

    rc, out = _git(["worktree", "add", "-B", branch, worktree_path, base_ref], workspace)
    if rc != 0:
        return _abort(f"ABORTADO: no se pudo crear el worktree {worktree_path}: {out.strip()[:300]}")

    _log(f"[git_setup] Worktree aislado: {worktree_path} en rama {branch} (base: {base})")

    # Los worktrees de git nunca comparten node_modules (está en .gitignore).
    # Instalar dependencias acá para que tsc/vitest existan en la validación.
    if os.path.exists(os.path.join(worktree_path, "package.json")):
        install_rc, install_out = _run(["npm", "install"], cwd=worktree_path, timeout=300)
        if install_rc != 0:
            # Un solo reintento antes de abortar — cubre fallos transitorios
            # (contención de red/cache si hay varios despachos concurrentes
            # sobre el mismo repo).
            _log(f"[git_setup] npm install falló, reintentando una vez: {install_out.strip()[:200]}")
            install_rc, install_out = _run(["npm", "install"], cwd=worktree_path, timeout=300)

        if install_rc != 0:
            _git(["worktree", "remove", "--force", worktree_path], workspace)
            _git(["worktree", "prune"], workspace)
            shutil.rmtree(worktree_path, ignore_errors=True)
            return _abort(
                f"ABORTADO: npm install falló dos veces en el worktree nuevo. "
                f"No tiene sentido continuar con dependencias parcialmente "
                f"instaladas — los ciclos de validación fallarían de forma "
                f"confusa sin indicar la causa real.\n\n{install_out.strip()[:500]}"
            )

        _log(f"[git_setup] npm install: ✓ OK ({install_out.strip()[:200]})")

    # Baseline de tests ANTES de que dynamic_developer edite nada (requerimiento
    # 10): permite que validation_gate distinga fallos preexistentes (no deben
    # bloquear un ticket acotado) de regresiones nuevas introducidas por este
    # despacho. None si vitest no aplica a este proyecto.
    stack = state.get("stack", "generic")
    baseline_failing_tests = _run_vitest_baseline(worktree_path, stack)
    if baseline_failing_tests is not None:
        _log(f"[git_setup] Baseline de tests: {len(baseline_failing_tests)} fallo(s) preexistente(s) antes del despacho")

    return {
        "workspace_path": worktree_path,
        "base_branch":    base,
        "work_branch":    branch,
        # None (no lista vacía) cuando vitest no aplica — distingue "no hay
        # baseline disponible" (cae al comportamiento estricto anterior) de
        # "baseline capturado y no había ningún fallo preexistente".
        "validation_baseline_failing_tests": (
            sorted(baseline_failing_tests) if baseline_failing_tests is not None else None
        ),
        "messages": [AIMessage(
            content=f"Worktree {worktree_path} creado en rama {branch} desde {base}",
            name="GitOps",
        )],
    }


def planner_node(state: FleetState) -> dict:
    """Descompone el ticket en subtareas ordenadas y verificables, ancladas al código real.

    Fragmentar evita el "code dump" monolítico que trunca y alucina: el desarrollador
    trabaja sobre una checklist concreta (p.ej. schema → modelo → spec → controller).
    """
    criteria  = state["acceptance_criteria"]
    stack     = state.get("stack", "generic")
    workspace = state["workspace_path"]

    grounding = _rails_grounding(workspace, criteria) if stack == "rails" else ""
    stack_hint = {
        "rails": (
            "Para Rails el orden típico es: migración/esquema → modelo → factory → spec → controller. "
            "OBLIGATORIO: incluye al menos una subtarea de tests (RSpec) para el comportamiento nuevo."
        ),
        "node": (
            "Para Next.js+Prisma el orden típico es: "
            "1) schema.prisma+migración, "
            "2) funciones puras en src/lib/ CON su *.test.ts (TDD: escribe el test antes), "
            "3) componentes/Server Actions, "
            "4) integración en páginas/forms, "
            "5) tests de integración (Server Actions / API routes), "
            "6) tests E2E en tests/e2e/*.spec.ts si hay flujos de UI. "
            "OBLIGATORIO: las subtareas de testing son parte del plan, no opcionales. "
            "Un cambio sin tests unitarios NO cumple los criterios de aceptación."
        ),
        "flutter": "Para Flutter: modelos → servicios → widgets → pantallas → tests (widget tests).",
    }.get(stack, "")
    sys = SystemMessage(content=(
        "Eres el Tech Lead. Descompón el ticket en 2 a 6 subtareas atómicas, ordenadas "
        "por dependencia y CADA una verificable (qué archivo se toca y cómo se comprueba). "
        f"{stack_hint} "
        "Responde SOLO un array JSON de strings, sin texto extra."
    ))
    human = HumanMessage(content=f"TICKET:\n{criteria}\n\n{grounding}\n\nDevuelve el array JSON de subtareas.")

    def _plan_once() -> list:
        raw = _invoke_reviewer([sys, human])
        content = raw.content if hasattr(raw, "content") else str(raw)
        content = _re.sub(r"<think>.*?</think>", "", content, flags=_re.DOTALL)
        lo, hi = content.find("["), content.rfind("]")
        arr = _json.loads(content[lo:hi + 1]) if lo != -1 and hi > lo else []
        return [str(x) for x in arr][:6]

    # Requerimiento 14: el planner devolvía 0 subtareas de forma silenciosa
    # (fallo transitorio de parseo del modelo revisor, o array vacío) y el
    # pipeline seguía sin guía. Un reintento cubre el caso transitorio; si
    # persiste, se deja un aviso EXPLÍCITO y se continúa con el fallback
    # intencional (el criteria completo) — tickets simples legítimamente no
    # necesitan descomposición, por eso no se aborta.
    subtasks: list = []
    for attempt in (1, 2):
        try:
            subtasks = _plan_once()
        except Exception as e:  # noqa: BLE001
            _log(f"[planner] Intento {attempt}: no se pudo descomponer ({e})")
            subtasks = []
        if subtasks:
            break
        if attempt == 1:
            _log("[planner] 0 subtareas en el primer intento — reintentando una vez")

    if subtasks:
        _log("[planner] Subtareas:\n" + "\n".join(f"  {i+1}. {t}" for i, t in enumerate(subtasks)))
    else:
        _log("[planner] ⚠ AVISO: el planner no generó subtareas tras 2 intentos — se "
             "procede con el ticket completo como guía (fallback). Si el ticket es "
             "grande/complejo, considera dividirlo en despachos más chicos.")
    return {
        "subtasks": subtasks,
        "messages": [AIMessage(content=f"{len(subtasks)} subtareas planificadas", name="TechLead")],
    }


def codebase_reader_node(state: FleetState) -> dict:
    """Lee los archivos existentes que el desarrollador va a modificar.

    Objetivo: que el LLM vea el contenido REAL antes de escribir, eliminando
    la causa raíz de las regresiones (ej: reemplazar schema.prisma completo).
    """
    workspace = state["workspace_path"]
    stack     = state.get("stack", "generic")
    subtasks  = state.get("subtasks", [])
    criteria  = state.get("acceptance_criteria", "")

    # Extraer rutas mencionadas en subtasks y criterios. Incluye `.md`: los
    # requerimientos en modo libre suelen referenciar un archivo de plan
    # (ej. `docs/superpowers/plans/*.md`) pidiéndole al agente que "lo lea" —
    # sin esto, ese plan NUNCA se lee de verdad ni se inyecta en el prompt del
    # LLM (una llamada de completion, no un agente con herramientas de
    # filesystem), y el modelo termina alucinando una versión propia del
    # contenido a partir del nombre/descripción (ver requerimiento 08).
    text = "\n".join(subtasks) + "\n" + criteria
    found = _re.findall(
        r'[\w/\-\.]+\.(?:ts|tsx|js|prisma|sql|rb|py|go|rs|json|yaml|yml|md)',
        text
    )
    candidates = list(dict.fromkeys(
        _KEY_FILES_BY_STACK.get(stack, []) + found
    ))

    existing: Dict[str, str] = {}
    for rel in candidates:
        full = os.path.join(workspace, rel)
        if os.path.isfile(full):
            try:
                with open(full, errors="replace") as fh:
                    existing[rel] = fh.read(80_000)
            except Exception:
                pass

    _log(f"[codebase_reader] {len(existing)} archivos existentes capturados: "
         + ", ".join(list(existing.keys())[:8])
         + (f" (+{len(existing)-8} más)" if len(existing) > 8 else ""))
    return {
        "existing_files":   existing,
        "regression_errors": [],
        "messages": [AIMessage(
            content=f"codebase_reader: {len(existing)} archivos leídos",
            name="CodebaseReader"
        )],
    }


def regression_guard_node(state: FleetState) -> dict:
    """Verifica que los archivos generados no eliminaron contenido existente,
    y RESTAURA de inmediato cualquier archivo donde detecte una regresión.

    Corre DESPUÉS del dynamic_developer y ANTES de validation_gate. Antes, una
    regresión detectada solo marcaba validation_passed=False y dejaba el
    archivo roto en disco mientras el ciclo de corrección lo intentaba
    arreglar — si el ciclo se agotaba sin éxito, el archivo truncado quedaba
    así permanentemente. Ahora, apenas se detecta una regresión en un archivo,
    ese archivo se restaura a su contenido previo a este ciclo (capturado por
    codebase_reader_node) ANTES de reportar el problema al developer — el
    working tree nunca queda peor de lo que estaba, sin importar cuántos
    ciclos de corrección hagan falta o si se agotan sin éxito.

    Nota: existing_files trunca a 80.000 caracteres (ver codebase_reader_node);
    para archivos más grandes que eso, la restauración también sería parcial.
    """
    workspace      = state["workspace_path"]
    existing_files = state.get("existing_files", {})
    applied_files  = set(state.get("applied_files", []))

    issues: List[str] = []
    restored: List[str] = []

    for rel, old_content in existing_files.items():
        if rel not in applied_files:
            continue  # archivo no tocado — sin riesgo

        full = os.path.join(workspace, rel)
        file_issues: List[str] = []

        if not os.path.exists(full):
            file_issues.append(f"ELIMINADO: {rel} fue borrado por el generador")
        else:
            try:
                with open(full, errors="replace") as fh:
                    new_content = fh.read()
            except Exception:
                continue

            if rel.endswith("schema.prisma"):
                file_issues.extend(_check_prisma_regression(old_content, new_content))
            elif rel.endswith((".ts", ".tsx", ".js")):
                file_issues.extend(
                    f"{rel}: {e}" for e in _check_ts_exports_regression(old_content, new_content)
                )

        if file_issues:
            issues.extend(file_issues)
            try:
                os.makedirs(os.path.dirname(full), exist_ok=True)
                with open(full, "w") as fh:
                    fh.write(old_content)
                restored.append(rel)
            except Exception as e:  # noqa: BLE001
                _log(f"[regression_guard] No se pudo restaurar {rel}: {e}")

    if issues:
        issues_text    = "\n".join(f"  - {i}" for i in issues)
        restored_text  = (
            "\n\nArchivos RESTAURADOS a su contenido previo a este ciclo (la "
            "versión con regresión NO quedó en disco): " + ", ".join(restored)
            if restored else ""
        )
        report = (
            "REGRESIÓN DETECTADA — el código generado eliminó elementos existentes:\n"
            f"{issues_text}"
            f"{restored_text}\n\n"
            "REGLA CRÍTICA: Cuando modificas un archivo existente, debes incluir TODO "
            "el contenido original (todos sus modelos, campos, funciones, exports) y "
            "solo AÑADIR los elementos nuevos. El bloque ARCHIVOS EXISTENTES del prompt "
            "muestra el contenido que DEBE aparecer en tu output — ese archivo fue "
            "restaurado ahí, vuelve a intentar el cambio sobre esa base."
        )
        _log(f"[regression_guard] ⚠ {len(issues)} regresión(es), {len(restored)} archivo(s) restaurado(s): {issues[:3]}")
        return {
            "regression_errors": issues,
            "validation_passed": False,
            "validation_report": report,
            "messages": [AIMessage(
                content=f"regression_guard: ⚠ {len(issues)} regresión(es) — {len(restored)} archivo(s) restaurado(s)",
                name="RegressionGuard"
            )],
        }

    _log(f"[regression_guard] ✓ sin regresiones ({len(existing_files)} archivos verificados)")
    return {
        "regression_errors": [],
        "messages": [AIMessage(content="regression_guard: ✓ sin regresiones", name="RegressionGuard")],
    }


@task
def _agent_generation(agent_role: str, system_instruction: str, human_instruction: str) -> str:
    """Invocación LLM de un agent_role como task checkpointeable (Req 5.2):
    LangGraph persiste el resultado individualmente, así una reanudación a
    mitad de ciclo omite los agentes ya completados (Req 5.3)."""
    response = _invoke_dev([SystemMessage(content=system_instruction),
                            HumanMessage(content=human_instruction)])
    return response.content


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
    new_diffs    = {}
    all_applied  = list(prev_applied)  # acumular entre ciclos
    all_rejected = []  # archivos rechazados este ciclo por la guarda anti-truncamiento

    cycle = state["loop_iterations"] + 1
    # Ciclo visible para los spans de invocación LLM (Req 6.4) vía thread-local
    # — el grafo corre síncrono en el thread del worker.
    _thread_local.current_cycle = cycle
    _log(f"[dynamic_developer] --- Ciclo {cycle} ---")
    if feedback and feedback != "Implementacion inicial requerida.":
        _log(f"[dynamic_developer] Feedback del revisor: {feedback[:150]}")

    stack       = state.get("stack", "generic")

    if stack == "node":
        FORMAT_EXAMPLE = (
            "FORMATO OBLIGATORIO — copia este patron para CADA archivo:\n\n"
            "===FILE_BEGIN: src/lib/ejemplo.ts===\n"
            "export function ejemplo(): string {\n"
            "  return 'hola';\n"
            "}\n"
            "===FILE_END===\n\n"
            "===FILE_BEGIN: src/components/ui/Ejemplo.tsx===\n"
            "'use client';\n"
            "export function Ejemplo() {\n"
            "  return <div>ejemplo</div>;\n"
            "}\n"
            "===FILE_END===\n"
        )
    else:
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
    subtasks       = state.get("subtasks", [])
    existing_files = state.get("existing_files", {})
    grounding      = _rails_grounding(workspace, criteria) if stack == "rails" else ""
    subtask_txt    = ("PLAN DE SUBTAREAS (impleméntalas todas, en orden):\n"
                      + "\n".join(f"  {i+1}. {t}" for i, t in enumerate(subtasks)) + "\n\n") if subtasks else ""

    # Contexto de archivos existentes: el LLM DEBE preservar todo su contenido
    if existing_files:
        existing_parts = [
            "ARCHIVOS EXISTENTES (DEBES PRESERVAR TODO SU CONTENIDO — incluye cada "
            "campo, modelo y función original + tus adiciones nuevas):"
        ]
        for rel, content in existing_files.items():
            existing_parts.append(f"\n===FILE_EXISTING: {rel}===\n{content}\n===FILE_EXISTING_END===")
        existing_files_ctx = "\n".join(existing_parts)
    else:
        existing_files_ctx = ""

    guardrails = _get_guardrails(stack)
    playbooks  = ROLE_PLAYBOOKS_NODE if stack == "node" else ROLE_PLAYBOOKS

    for agent_role in agents:
        playbook = playbooks.get(agent_role, playbooks.get("Full-Stack", ROLE_PLAYBOOKS["Full-Stack"]))
        system_instruction = (
            f"Eres un experto en {agent_role}. {playbook}\n"
            "Tu unica tarea es escribir codigo. NO expliques nada. NO describas lo que vas a hacer.\n"
            "SOLO escribe los archivos usando el formato especificado.\n\n"
            + guardrails + "\n\n"
            "CRITICO — REGLAS ANTI-TRUNCACION Y ANTI-REGRESION:\n"
            "1. Cada archivo debe estar COMPLETO desde la primera hasta la ultima linea.\n"
            "2. NUNCA uses '...', '# rest of implementation', '// TODO', '# continua...' ni ningun placeholder.\n"
            "3. Si un archivo seria muy grande, divídelo en archivos mas pequeños cohesivos en lugar de truncar.\n"
            "4. El ultimo caracter de cada bloque FILE_BEGIN/FILE_END debe ser el cierre real del archivo.\n"
            "5. NO escapes comillas ni saltos de linea: escribe el archivo tal cual debe quedar en disco.\n"
            "6. ANTI-REGRESION: Si modificas un archivo existente (ver ARCHIVOS EXISTENTES abajo), "
            "DEBES incluir TODO su contenido original. Ningun campo, modelo ni funcion puede desaparecer.\n\n"
            + FORMAT_EXAMPLE
        )
        human_instruction = (
            f"CRITERIOS DEL TICKET:\n{criteria}\n\n"
            f"{subtask_txt}"
            f"{grounding}\n\n"
            f"RESULTADO DE LA VALIDACION/REVISION ANTERIOR (corrige ESTO primero):\n{feedback}\n\n"
            f"{existing_files_ctx}\n\n"
            f"CONTEXTO DEL WORKSPACE:\n{workspace_context}\n\n"
            f"ARCHIVOS YA EN DISCO: {prev_applied}\n\n"
            "Escribe ahora TODOS los archivos necesarios usando bloques "
            "===FILE_BEGIN: ruta=== ... ===FILE_END===\n"
            "Empieza directamente con el primer ===FILE_BEGIN==="
        )

        _log(f"[dynamic_developer] Agente '{agent_role}': llamando al modelo LLM...")
        # @task (Req 5.2/5.3): el resultado por agent_role se checkpointea
        # individualmente — al reanudar a mitad de un ciclo con varios agentes,
        # los roles ya completados no se re-invocan (ni se re-cobran).
        generation = _agent_generation(agent_role, system_instruction, human_instruction)
        # `hasattr` en vez de isinstance: con el langgraph real devuelve un
        # future; en tests con stubs (task = identidad) devuelve el str directo.
        response_text = generation.result() if hasattr(generation, "result") else generation
        new_diffs[agent_role] = response_text

        blocks = len(_re.findall(r"===FILE_BEGIN:", response_text))
        _log(f"[dynamic_developer] Respuesta recibida ({len(response_text)} chars, {blocks} bloques FILE_BEGIN detectados)")

        try:
            applied, rejected = _apply_workspace_changes(workspace, response_text, criteria=criteria)
            for f in applied:
                if f not in all_applied:
                    all_applied.append(f)
            all_rejected.extend(rejected)
            if applied:
                _log(f"[dynamic_developer] Archivos escritos: {', '.join(applied[:8])}" +
                     (f" (+{len(applied)-8} más)" if len(applied) > 8 else ""))
                logger.info("Agente %s aplico cambios en: %s", agent_role, applied)
            else:
                _log(f"[dynamic_developer] AVISO: el agente '{agent_role}' no generó bloques FILE_BEGIN/END válidos")
                logger.warning("Agente %s no genero bloques FILE_BEGIN/END", agent_role)
            if rejected:
                _log(f"[dynamic_developer] RECHAZADOS por guarda anti-truncamiento: {'; '.join(rejected)}")
                logger.warning("Agente %s: archivos rechazados por truncamiento: %s", agent_role, rejected)
        except Exception as e:
            _log(f"[dynamic_developer] ERROR aplicando archivos: {e}")
            logger.error("Error aplicando cambios del agente %s: %s", agent_role, e)

    summary_msg = (
        f"Ciclo {cycle} completado. "
        f"Total archivos en disco: {len(all_applied)}"
        + (f". {len(all_rejected)} rechazados por truncamiento." if all_rejected else "")
    )
    _log(f"[dynamic_developer] Ciclo {cycle} completado — {len(all_applied)} archivos totales en disco")
    return {
        "current_code_diff": new_diffs,
        "applied_files":     all_applied,
        "rejected_files":    all_rejected,
        "loop_iterations":   cycle,
        "messages":          [AIMessage(content=summary_msg, name="DevFleet")],
    }


def _read_applied_files(workspace: str, applied_files: list, max_bytes: int = 3000) -> str:
    """Lee el contenido actual de los archivos que realmente fueron escritos al disco."""
    if not applied_files:
        return "(ningún archivo fue escrito al disco)"
    # Presupuesto dinámico: más bytes por archivo cuando hay pocos archivos
    n = min(len(applied_files), 40)
    budget_per_file = min(max(max_bytes, 200_000 // max(n, 1)), 40_000)
    parts = [f"# Archivos creados/modificados ({len(applied_files)} total):"]
    for rel_path in applied_files[:40]:
        full_path = os.path.join(workspace, rel_path)
        try:
            with open(full_path, "r", errors="replace") as fh:
                content = fh.read(budget_per_file)
            truncated = "" if len(content) < budget_per_file else "\n[... archivo truncado por límite de lectura ...]"
            parts.append(f"\n===FILE: {rel_path}===\n{content}{truncated}\n===END===")
        except Exception as e:
            parts.append(f"\n[No se pudo leer {rel_path}: {e}]")
    return "\n".join(parts)


def validation_gate_node(state: FleetState) -> dict:
    """Gate DETERMINISTA: ejecuta el código real (sintaxis + tests del proyecto).

    Es la corrección central a la causa raíz de esta epopeya: antes, la única
    "revisión" era un LLM leyendo archivos truncados → aprobaba código que ni
    siquiera parseaba o booteaba. Ahora ningún cambio avanza sin pasar checks
    ejecutables. El reporte (errores reales) se realimenta al desarrollador.
    """
    workspace     = state["workspace_path"]
    applied_files = state.get("applied_files", [])
    stack         = state.get("stack", "generic")

    if not applied_files:
        _log("[validation_gate] Sin archivos que validar.")
        return {"validation_passed": False,
                "validation_report": "No se generó ningún archivo.",
                "messages": [AIMessage(content="Validación: sin archivos", name="Validator")]}

    # Si regression_guard ya detectó regresiones, no correr tsc — el reporte ya está listo
    regression_errors = state.get("regression_errors", [])
    if regression_errors:
        report = state.get("validation_report", "(reporte de regresión)")
        _log(f"[validation_gate] Regresiones pendientes — saltando tsc ({len(regression_errors)} issues)")
        return {
            "validation_passed": False,
            "validation_report": report,
            "messages": [AIMessage(
                content=f"Validación: REGRESIÓN ({len(regression_errors)} issues)",
                name="Validator"
            )],
        }

    baseline = state.get("validation_baseline_failing_tests")
    baseline_set = set(baseline) if baseline is not None else None

    _log(f"[validation_gate] Validando {len(applied_files)} archivos (stack={stack})...")
    passed, report = _validate_workspace(workspace, applied_files, stack, baseline_failing_tests=baseline_set)
    verdict = "PASÓ ✓" if passed else "FALLÓ ✗"
    _log(f"[validation_gate] {verdict}\n{report[:400]}")
    return {
        "validation_passed": passed,
        "validation_report": report,
        "messages": [AIMessage(content=f"Validación determinista: {verdict}", name="Validator")],
    }


def reviewer_node(state: FleetState) -> dict:
    """Evalua si los archivos escritos en el workspace cumplen los criterios de aceptacion."""
    workspace      = state["workspace_path"]
    criteria       = state["acceptance_criteria"]
    applied_files  = state.get("applied_files", [])

    _log(f"[quality_reviewer] Revisando {len(applied_files)} archivos contra criterios de aceptación...")

    # Fast-reject: un nodo previo agotó sus reintentos y el error_handler
    # global marcó el fallo (Req 4.3) — no gastar el LLM revisor; el router
    # desviará al cierre ordenado.
    if state.get("aborted"):
        _log("[quality_reviewer] RECHAZO RÁPIDO: ciclo abortado por fallo de nodo (error_handler)")
        return {
            "reviewer_feedback": state.get("reviewer_feedback", "Ciclo abortado por fallo de un nodo."),
            "is_approved": False,
        }

    # Fast-reject: la guarda anti-truncamiento de _apply_workspace_changes marcó
    # algún archivo como rechazado (reescritura masiva de un archivo existente
    # grande). Nunca dejar que el LLM reviewer "apruebe" un ciclo así — el
    # riesgo es justamente que el reviewer no note la pérdida de datos (ver
    # requerimiento 06).
    rejected_files = state.get("rejected_files", [])
    if rejected_files:
        _log(f"[quality_reviewer] RECHAZO RÁPIDO: guarda anti-truncamiento detectó {len(rejected_files)} archivo(s)")
        logger.warning("Reviewer fast-reject: archivos rechazados por truncamiento: %s", rejected_files)
        return {
            "reviewer_feedback": (
                "Uno o más archivos fueron rechazados porque el contenido generado "
                "perdió más del 30% de sus líneas/bytes respecto al original "
                "(posible truncamiento o reescritura no solicitada):\n\n"
                + "\n".join(rejected_files)
                + "\n\nPara archivos grandes NO regeneres el archivo completo: usa un "
                "enfoque de edición dirigida (describe los cambios línea por línea) o, "
                "si el archivo es demasiado grande para reproducirlo íntegro, divide el "
                "trabajo en ediciones más pequeñas."
            ),
            "is_approved": False,
        }

    # Fast-reject: si el desarrollador no escribió ningún archivo, no llamar al LLM
    if not applied_files:
        _log("[quality_reviewer] RECHAZO RÁPIDO: no se generó ningún archivo con formato FILE_BEGIN/END")
        logger.warning("Reviewer fast-reject: el desarrollador no generó ningún archivo en esta iteración.")
        return {
            "reviewer_feedback": "El desarrollador no generó ningún archivo con el formato FILE_BEGIN/END. Debes usar EXACTAMENTE el formato especificado y escribir TODOS los archivos del servicio.",
            "is_approved": False,
        }

    # Gate determinista primero: si el código no parsea / los tests fallan, NO se
    # gasta el LLM — se devuelven los errores reales para que el dev los corrija.
    if not state.get("validation_passed", False):
        report = state.get("validation_report", "(sin reporte)")
        _log("[quality_reviewer] RECHAZO por validación determinista (errores reales devueltos al dev)")
        return {
            "reviewer_feedback": ("La validación determinista FALLÓ. Corrige EXACTAMENTE estos errores "
                                  "antes de cualquier otra cosa:\n\n" + report),
            "is_approved": False,
        }

    # Leer el contenido REAL de los archivos escritos (no el workspace genérico)
    files_content = _read_applied_files(workspace, applied_files)
    _log(f"[quality_reviewer] Archivos a revisar: {', '.join(applied_files[:6])}" +
         (f" (+{len(applied_files)-6} más)" if len(applied_files) > 6 else ""))
    _log("[quality_reviewer] Llamando al modelo revisor...")

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
            f"VALIDACION DETERMINISTA (ya pasó: sintaxis/tests):\n{state.get('validation_report', '(n/a)')[:1500]}\n\n"
            f"ARCHIVOS ESCRITOS AL DISCO:\n{files_content}\n\n"
            "Los tests ya pasan. Evalúa si la solución CUMPLE SEMÁNTICAMENTE los criterios "
            "(no solo que compile). Emite tu dictamen en JSON."
        )
    )

    decision: ReviewerDecision = _invoke_reviewer_structured([sys_prompt, human_prompt])

    verdict = "APROBADO ✓" if decision.is_approved else "RECHAZADO ✗"
    _log(f"[quality_reviewer] {verdict}: {decision.corrective_feedback[:200]}")

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


def git_finalize_node(state: FleetState) -> dict:
    """GitFlow: commitea el trabajo en la rama del ticket, hace push y abre PR.

    NUNCA mergea a main: deja el trabajo en una rama + PR para revisión humana.
    Aprobado o no, el código vive aislado en su rama (revertible). Si la validación
    no pasó, igual commitea para inspección manual pero marca el PR/commit como WIP.
    """
    workspace = state["workspace_path"]
    ticket    = state["ticket_id"]
    branch    = state.get("work_branch", "")
    approved  = state.get("is_approved", False) and state.get("validation_passed", False)
    applied   = state.get("applied_files", [])

    if not branch or not _is_git_repo(workspace):
        # No debería ocurrir: git_setup aborta el grafo entero si no hay repo git
        # o el árbol estaba sucio. Guardia defensiva por si el estado llega roto.
        _log("[git_finalize] Sin rama de trabajo; se omite commit/PR")
        return {"pr_url": "", "messages": [AIMessage(content="git omitido", name="GitOps")]}

    if not applied:
        _log("[git_finalize] No hay archivos que commitear")
        return {"pr_url": "", "messages": [AIMessage(content="nada que commitear", name="GitOps")]}

    status = "" if approved else "[WIP] "
    title  = state.get("acceptance_criteria", "").split("\n")[0].replace("TITULO:", "").strip()[:72]
    body   = (f"{status}{ticket}: {title}\n\n"
              f"Generado por la Flota de Agentes.\n"
              f"Ciclos: {state.get('loop_iterations', 0)} · Archivos: {len(applied)} · "
              f"Validación: {'PASÓ' if state.get('validation_passed') else 'FALLÓ'} · "
              f"Revisor: {'APROBÓ' if state.get('is_approved') else 'RECHAZÓ'}\n\n"
              f"Resumen revisor:\n{state.get('reviewer_feedback', '')[:500]}\n\n"
              f"Ticket: {ticket}")

    _git(["add", "-A"], workspace)
    rc, out = _git(["commit", "-m", body], workspace)
    if rc != 0 and "nothing to commit" not in out.lower():
        _log(f"[git_finalize] commit falló: {out.strip()[:160]}")
        return {"pr_url": "", "messages": [AIMessage(content=f"commit falló: {out.strip()[:120]}", name="GitOps")]}
    _log(f"[git_finalize] Commit en {branch} ({len(applied)} archivos)")

    rc, out = _git(["push", "-u", "origin", branch], workspace, timeout=180)
    if rc != 0:
        _log(f"[git_finalize] push falló (¿sin remoto/credenciales?): {out.strip()[:160]}")
        return {"pr_url": "", "messages": [AIMessage(content=f"push falló: {out.strip()[:120]}", name="GitOps")]}

    # PR vía gh CLI si está disponible; si no, deja la rama empujada (PR manual).
    pr_url = ""
    if _tool_available("gh"):
        base = state.get("base_branch", "main") or "main"
        rc, out = _run(["gh", "pr", "create", "--base", base, "--head", branch,
                        "--title", f"{status}{ticket}: {title}", "--body", body],
                       cwd=workspace, timeout=120)
        m = _re.search(r"https?://\S+/pull/\d+", out)
        if m:
            pr_url = m.group(0)
            _log(f"[git_finalize] PR abierto: {pr_url}")
        else:
            _log(f"[git_finalize] gh pr create sin URL: {out.strip()[:160]}")
    else:
        _log(f"[git_finalize] gh no disponible; rama '{branch}' empujada (abre el PR manualmente)")

    return {"pr_url": pr_url, "messages": [AIMessage(content=f"Rama {branch} empujada{' · PR ' + pr_url if pr_url else ''}", name="GitOps")]}


def staging_tester_node(state: FleetState) -> dict:
    """Smoke tests + E2E contra staging. No-op si no hay URL configurada."""
    workspace = state.get("workspace_path", "")
    report: list[str] = []
    ok = True

    # ── 1. Determinar la URL de staging ────────────────────────────────────
    staging_url = os.getenv("STAGING_BASE_URL", "").strip()

    if not staging_url:
        # Intentar deploy automático a Vercel si el proyecto tiene .vercel/project.json
        vercel_cfg = os.path.join(workspace, ".vercel", "project.json")
        if os.path.exists(vercel_cfg) and _tool_available("vercel"):
            _log("[staging_tester] .vercel/project.json detectado — desplegando a staging…")
            rc, out = _run(
                ["vercel", "deploy", "--target", "staging", "--yes", "--no-wait"],
                cwd=workspace, timeout=int(os.getenv("FLEET_STAGING_DEPLOY_TIMEOUT", "300")),
            )
            if rc == 0:
                m = _re.search(r'https://[^\s]+\.vercel\.app', out)
                if m:
                    staging_url = m.group(0).strip()
                    _log(f"[staging_tester] preview URL: {staging_url}")
            else:
                report.append(f"STAGING DEPLOY: ✗ vercel deploy falló (exit {rc})\n{out[-500:]}")
                ok = False

    if not staging_url:
        msg = (
            "STAGING: ⚠ URL no configurada — se omite el test de staging.\n"
            "  Para activarlo: define STAGING_BASE_URL=<url> en .env\n"
            "  o asegúrate de que .vercel/project.json exista y `vercel` CLI esté instalado."
        )
        _log(f"[staging_tester] {msg}")
        return {
            "staging_url":    "",
            "staging_passed": True,   # No bloqueamos si staging no está configurado
            "staging_report": msg,
            "messages": [AIMessage(content="staging omitido — URL no configurada", name="StagingTester")],
        }

    report.append(f"STAGING TEST → {staging_url}\n")

    # ── 2. Migraciones de DB en staging (stack node con Prisma) ────────────
    # Se aplican ANTES de los smoke tests para que la DB esté al día.
    staging_env_file = os.path.join(workspace, ".env.staging")
    prisma_bin = os.path.join(workspace, "node_modules", ".bin", "prisma")
    if (
        state.get("stack") == "node"
        and os.path.exists(staging_env_file)
        and os.path.exists(prisma_bin)
    ):
        _log("[staging_tester] aplicando migraciones en staging DB…")
        rc_mig, out_mig = _run(
            ["node", "--env-file=.env.staging", prisma_bin, "migrate", "deploy"],
            cwd=workspace,
            timeout=120,
        )
        if rc_mig == 0:
            report.append("  ✓ Migraciones staging DB: aplicadas correctamente")
        else:
            report.append(f"  ✗ Migraciones staging DB: falló (exit {rc_mig})\n{out_mig[-300:]}")
            ok = False

    # ── 3. Smoke tests de API (curl, sin autenticación) ─────────────────────
    smoke_checks = [
        ("/",                                     ["200", "301", "302"], "home"),
        ("/api/mobile/admin/support-data",        ["401", "307", "403"], "support-data (sin auth)"),
    ]
    if state.get("stack") == "node":
        smoke_checks.append(("/api/health", ["200", "404"], "health endpoint"))

    for path, expected_codes, label in smoke_checks:
        url = staging_url.rstrip("/") + path
        rc, out = _run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
             "--connect-timeout", "10", "--max-time", "15", "-L", url],
            cwd="/", timeout=20,
        )
        code = out.strip()
        if code in expected_codes:
            report.append(f"  ✓ {label}: HTTP {code}")
        else:
            report.append(f"  ✗ {label}: esperaba {expected_codes}, obtuvo '{code}'")
            ok = False

    # ── 4. Playwright E2E contra la URL de staging ──────────────────────────
    e2e_dir = os.path.join(workspace, "tests", "e2e")
    playwright_bin = os.path.join(workspace, "node_modules", ".bin", "playwright")
    if os.path.isdir(e2e_dir) and os.path.exists(playwright_bin):
        _log(f"[staging_tester] ejecutando E2E de Playwright contra {staging_url}")
        env_with_url = {**os.environ, "PLAYWRIGHT_BASE_URL": staging_url}
        try:
            p = subprocess.run(
                ["node", playwright_bin, "test", "--reporter=list", "--project=chromium"],
                cwd=workspace,
                capture_output=True,
                text=True,
                timeout=int(os.getenv("FLEET_STAGING_E2E_TIMEOUT", "300")),
                env=env_with_url,
            )
            tail = "\n".join((p.stdout + p.stderr).strip().splitlines()[-30:])
            if p.returncode == 0:
                report.append(f"  ✓ Playwright E2E: todos los tests pasan\n{tail}")
            else:
                report.append(f"  ✗ Playwright E2E: tests fallando (exit {p.returncode})\n{tail}")
                ok = False
        except subprocess.TimeoutExpired:
            report.append("  ✗ Playwright E2E: TIMEOUT — los tests tardaron demasiado")
            ok = False
    else:
        report.append("  ⚠ Playwright E2E: omitido (playwright no instalado en node_modules o sin tests/e2e/)")

    full_report = "\n".join(report)
    verdict = "✓ STAGING PASÓ" if ok else "✗ STAGING FALLÓ"
    _log(f"[staging_tester] {verdict}")
    return {
        "staging_url":    staging_url,
        "staging_passed": ok,
        "staging_report": full_report,
        "messages": [AIMessage(content=f"{verdict}\n{full_report}", name="StagingTester")],
    }


def finalize_and_update_jira(state: FleetState) -> dict:
    ticket_id    = state["ticket_id"]
    feedback     = state["reviewer_feedback"]
    iterations   = state["loop_iterations"]
    applied      = state.get("applied_files", [])
    is_approved  = state.get("is_approved", False)
    val_passed   = state.get("validation_passed", False)
    pr_url       = state.get("pr_url", "")
    branch       = state.get("work_branch", "")
    # "Hecho" SOLO si validación determinista pasó Y el revisor aprobó. Si no, va a
    # revisión humana (nunca se marca como terminado código que no pasó los tests).
    staging_passed = state.get("staging_passed", True)
    staging_url    = state.get("staging_url", "")
    staging_report = state.get("staging_report", "")
    done = is_approved and val_passed and staging_passed
    status_parts = []

    result_label = (
        "APROBADO+VALIDADO+STAGING" if done
        else f"REQUIERE REVISIÓN (val={'ok' if val_passed else 'falló'}, staging={'ok' if staging_passed else 'falló'}, max ciclos={iterations})"
    )
    _log(f"[jira_updater] Finalizando — resultado: {result_label}")

    # Modo requerimiento libre (sin Jira): el entregable es la rama + PR; no hay
    # ticket que comentar/transicionar.
    if (state.get("requirement") or "").strip() or jira_client is None:
        msg = (f"Finalizado ({result_label}). "
               + (f"PR: {pr_url}" if pr_url else (f"Rama: {branch}" if branch else "sin git"))
               + " · (sin Jira)")
        _log(f"[jira_updater] {msg}")
        return {"messages": [AIMessage(content=msg, name="Done")]}

    comment = (
        f"Implementacion procesada por la Flota de Agentes\n\n"
        f"Ciclos de revision: {iterations}\n"
        f"Archivos modificados: {len(applied)}\n"
        f"Validación determinista (sintaxis/tests): {'PASÓ ✓' if val_passed else 'FALLÓ ✗'}\n"
        f"Revisor semántico: {'APROBÓ ✓' if is_approved else 'RECHAZÓ ✗'}\n"
        f"Staging tests: {'PASÓ ✓' if staging_passed else 'FALLÓ ✗'}"
        + (f" — {staging_url}" if staging_url else " (no configurado)") + "\n"
        + (f"Rama: {branch}\n" if branch else "")
        + (f"Pull Request: {pr_url}\n" if pr_url else "")
        + (f"\nResultado staging:\n{staging_report}\n" if staging_url and not staging_passed else "")
        + f"\nResultado del revisor:\n{feedback}"
    )
    _log(f"[jira_updater] Añadiendo comentario en {ticket_id}...")
    try:
        jira_client.issue_add_comment(ticket_id, comment)
        status_parts.append("comentario añadido")
        _log(f"[jira_updater] Comentario añadido OK")
    except Exception as e:
        logger.warning("No se pudo comentar en %s: %s", ticket_id, e)
        status_parts.append(f"comentario omitido ({e})")
        _log(f"[jira_updater] No se pudo comentar: {e}")

    _log(f"[jira_updater] Buscando transición de estado...")
    try:
        transitions     = jira_client.get_issue_transitions(ticket_id)
        # Aprobado+validado → estados terminales/review; si no → solo a "en revisión"
        # (jamás marcar como hecho código que no pasó los tests).
        if done:
            target_keywords = ["in review", "en revisión", "done", "resolved", "closed", "hecho", "resuelto", "finalizado"]
        else:
            target_keywords = ["in review", "en revisión", "revisión"]
        for transition in transitions:
            to_value    = transition.get("to", "")
            target_name = (to_value if isinstance(to_value, str) else to_value.get("name", "")).lower()
            if any(kw in target_name for kw in target_keywords):
                jira_client.set_issue_status_by_transition_id(ticket_id, transition["id"])
                status_parts.append(f"transicionado a '{target_name}'")
                _log(f"[jira_updater] Ticket transicionado a '{target_name}' ✓")
                break
    except Exception as e:
        logger.warning("No se pudo transicionar %s: %s", ticket_id, e)
        status_parts.append(f"transicion omitida ({e})")
        _log(f"[jira_updater] No se pudo transicionar: {e}")

    msg = "Jira: " + "; ".join(status_parts) if status_parts else "Jira actualizado."
    _log(f"[jira_updater] Listo. {msg}")
    return {"messages": [AIMessage(content=msg, name="JiraOps")]}


# ===========================================================================
# 5. Parada grácil iniciada por el usuario
# ===========================================================================
def stop_gracefully(ticket_id: str, current_phase: str, iterations: int, applied_files: list) -> None:
    """Comenta el estado actual en Jira y transiciona el ticket a Bloqueado.
    No-op si no hay Jira (modo requerimiento libre)."""
    if jira_client is None:
        logger.info("stop_gracefully: sin Jira; nada que actualizar.")
        return
    files_list = "\n".join(f"  - {f}" for f in applied_files) if applied_files else "  (ninguno)"
    comment = (
        f"⚠️ *Ejecución detenida por el usuario.*\n\n"
        f"*Fase al detenerse:* {current_phase or '—'}\n"
        f"*Ciclos completados:* {iterations}\n"
        f"*Archivos escritos al disco:* {len(applied_files)}\n"
        f"{files_list}\n\n"
        f"El ticket fue movido a *Bloqueado* para revisión manual."
    )
    try:
        jira_client.issue_add_comment(ticket_id, comment)
    except Exception as e:
        logger.warning("stop_gracefully: no se pudo comentar en %s: %s", ticket_id, e)

    try:
        transitions = jira_client.get_issue_transitions(ticket_id)
        for transition in transitions:
            to_value = transition.get("to", "")
            name = (to_value if isinstance(to_value, str) else to_value.get("name", "")).lower()
            if "bloquead" in name or "blocked" in name:
                jira_client.set_issue_status_by_transition_id(ticket_id, transition["id"])
                logger.info("stop_gracefully: %s → Bloqueado", ticket_id)
                return
        logger.warning("stop_gracefully: no se encontró transición a Bloqueado en %s", ticket_id)
    except Exception as e:
        logger.warning("stop_gracefully: no se pudo transicionar %s: %s", ticket_id, e)


# ===========================================================================
# 7. Enrutador condicional (quality gate)
# ===========================================================================
def quality_gate_router(state: FleetState) -> str:
    """Avanza a finalizar SOLO si validación determinista pasó Y el revisor aprobó.
    Si no, reitera el ciclo dev→validación→review hasta agotar 6 ciclos.

    Cortes adicionales hacia el cierre ordenado (git_finalize → jira_updater):
    - `aborted`: un nodo agotó sus reintentos y el error_handler global marcó
      el fallo — no tiene sentido seguir iterando (Req 4.3).
    - `remaining_steps < 8`: no queda presupuesto de super-steps para otro
      ciclo completo (~4 pasos) más el camino de cierre (~3 pasos); cerrar
      ordenadamente en vez de morir con GraphRecursionError (Req 7.4).
    """
    approved = state.get("is_approved", False) and state.get("validation_passed", False)
    if state.get("aborted"):
        return "git_finalize"
    if state.get("remaining_steps", 999) < 8:
        _log(f"[quality_gate] remaining_steps={state.get('remaining_steps')} — cierre ordenado por límite de recursión")
        return "git_finalize"
    if state["loop_iterations"] >= 6 or approved:
        return "git_finalize"
    return "dynamic_developer"


# ===========================================================================
# 8. Construccion del grafo
# ===========================================================================
def _node_error_handler(state, error: NodeError) -> dict:
    """Recuperación tras agotar los reintentos de un nodo (Req 4.3): en vez de
    una excepción que mata el worker con estado a medias, marca el fallo en el
    estado y deja que quality_gate_router desvíe al cierre ordenado — el
    trabajo parcial queda commiteado en su rama para inspección.

    IMPORTANTE (requerimiento 14): el parámetro `error` DEBE estar anotado con
    `NodeError` — LangGraph inyecta el contexto del fallo por tipo de
    anotación, no por posición. Sin la anotación, el handler se invoca como un
    StateNode normal (solo `state`) y falla con "missing 1 required positional
    argument: 'error'", enmascarando la excepción original."""
    node = getattr(error, "node", "?")
    err = getattr(error, "error", error)
    msg = f"ABORTADO: el nodo {node} falló tras agotar reintentos: {str(err)[:400]}"
    _log(f"[error_handler] {msg}")
    return {
        "aborted":           True,
        "is_approved":       False,
        "validation_passed": False,
        "reviewer_feedback": msg,
        "messages":          [AIMessage(content=msg, name="ErrorHandler")],
    }


def _traced_node(fn: Callable, name: str) -> Callable:
    """Envuelve un nodo con un span OTel (Req 6.3). Si el tracing está
    deshabilitado, node_span es un no-op sin overhead observable."""
    def wrapper(state):
        with fleet_tracing.node_span(name):
            return fn(state)
    wrapper.__name__ = getattr(fn, "__name__", name)
    return wrapper


def build_architecture() -> StateGraph:
    graph = StateGraph(FleetState)
    # Tolerancia a fallos global (Req 4.1, 4.2): 2 intentos por nodo (el
    # retry_on default ya excluye ValueError/TypeError — bugs de programación,
    # Req 4.5/4.6) y handler de recuperación al agotarlos.
    graph.set_node_defaults(
        retry_policy=RetryPolicy(max_attempts=2),
        error_handler=_node_error_handler,
    )
    graph.add_node("context_ingestion",  _traced_node(fetch_and_plan_node, "context_ingestion"))
    graph.add_node("git_setup",          _traced_node(git_setup_node, "git_setup"))
    graph.add_node("planner",            _traced_node(planner_node, "planner"))
    graph.add_node("codebase_reader",    _traced_node(codebase_reader_node, "codebase_reader"))    # lee archivos antes de modificar
    # dynamic_developer con 1 solo intento (Req 4.4): sus llamadas LLM ya
    # tienen reintentos multicapa en _invoke_chain (backoff de gateway +
    # cadena de fallback de modelos); un retry a nivel nodo duplicaría todo
    # ese trabajo y el costo de tokens.
    graph.add_node("dynamic_developer",  _traced_node(dynamic_developer_node, "dynamic_developer"),
                   retry_policy=RetryPolicy(max_attempts=1))
    graph.add_node("regression_guard",   _traced_node(regression_guard_node, "regression_guard"))   # detecta regresiones post-generación
    graph.add_node("validation_gate",    _traced_node(validation_gate_node, "validation_gate"))
    graph.add_node("quality_reviewer",   _traced_node(reviewer_node, "quality_reviewer"))
    graph.add_node("git_finalize",       _traced_node(git_finalize_node, "git_finalize"))
    graph.add_node("staging_tester",     _traced_node(staging_tester_node, "staging_tester"))      # smoke tests + E2E contra staging
    graph.add_node("jira_updater",       _traced_node(finalize_and_update_jira, "jira_updater"))

    graph.add_edge(START, "context_ingestion")
    graph.add_edge("context_ingestion", "git_setup")
    graph.add_conditional_edges(
        "git_setup",
        lambda state: "aborted" if state.get("aborted") else "continue",
        {"aborted": END, "continue": "planner"},
    )
    graph.add_edge("planner",           "codebase_reader")         # captura archivos existentes
    graph.add_edge("codebase_reader",   "dynamic_developer")
    graph.add_edge("dynamic_developer", "regression_guard")        # verifica regresiones antes de tsc
    graph.add_edge("regression_guard",  "validation_gate")
    graph.add_edge("validation_gate",   "quality_reviewer")
    graph.add_conditional_edges(
        "quality_reviewer",
        quality_gate_router,
        {"dynamic_developer": "dynamic_developer", "git_finalize": "git_finalize"},
    )
    graph.add_edge("git_finalize",   "staging_tester")             # deploy + smoke + E2E en staging
    graph.add_edge("staging_tester", "jira_updater")
    graph.add_edge("jira_updater",   END)
    # Checkpointer durable (Req 1.1): el estado se persiste tras cada
    # super-step; con thread_id=job_id un job interrumpido se reanuda desde
    # el último checkpoint en vez de redespachar desde cero.
    return graph.compile(checkpointer=_get_checkpointer())


# ===========================================================================
# 9. Entrypoint — invocado por fleet_api.py o directo via CLI
# ===========================================================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Flota multi-agente LangGraph (Jira o requerimiento libre)")
    parser.add_argument("--ticket",      help="ID del ticket de Jira (ej. SCRUM-28)")
    parser.add_argument("--requirement", help="Requerimiento libre en texto (sin Jira). Cualquier proyecto.")
    parser.add_argument("--workspace",   required=True, help="Ruta al directorio del proyecto")
    parser.add_argument("--agents",      default="", help="Roles separados por coma (ej. Rails,Schema)")
    args = parser.parse_args()

    if not args.ticket and not args.requirement:
        parser.error("Debes indicar --ticket o --requirement")

    logging.basicConfig(level=logging.INFO)
    engine = build_architecture()
    initial_payload: FleetState = {
        "messages":            [],
        "ticket_id":           args.ticket or f"TASK-{os.urandom(4).hex()}",
        "requirement":         args.requirement or "",
        "workspace_path":      args.workspace,
        "acceptance_criteria": "",
        "required_agents":     [a.strip() for a in args.agents.split(",") if a.strip()],
        "current_code_diff":   {},
        "applied_files":       [],
        "reviewer_feedback":   "",
        "is_approved":         False,
        "loop_iterations":     0,
        "stack":               "",
        "base_branch":         "",
        "work_branch":         "",
        "subtasks":            [],
        "validation_report":   "",
        "validation_passed":   False,
        "pr_url":              "",
        "existing_files":      {},
        "regression_errors":   [],
        "staging_url":         "",
        "staging_passed":      True,
        "staging_report":      "",
    }

    print(f"\nIniciando flota para: {args.ticket or args.requirement[:60]}")
    cli_job_id = f"cli-{os.urandom(6).hex()}"
    for step_event in engine.stream(initial_payload, invoke_config(cli_job_id), stream_mode="updates"):
        for node_name, data in step_event.items():
            # Los @task también emiten eventos con valor crudo (no dict) — saltarlos.
            if not isinstance(data, dict):
                continue
            if data.get("messages"):
                print(f"\n[{node_name}] -> {data['messages'][-1].content}")

    print("\nFlota completada.")
