"""
Regresión para el requerimiento "_apply_workspace_changes debe detectar y
rechazar truncamientos masivos al reescribir un archivo existente grande"
(ver requerimientos/06-truncamiento-silencioso-archivos-grandes-full-rewrite.md).

Mismo patrón que los otros tests de langgraph_fleet: carga el módulo real por
ruta de archivo, stubbeando solo las dependencias externas pesadas.
"""
import importlib.util
import os
import sys
import types

import pytest


def _stub_module(name: str, **attrs) -> types.ModuleType:
    mod = sys.modules.get(name)
    if mod is None:
        mod = types.ModuleType(name)
        sys.modules[name] = mod
    for key, value in attrs.items():
        setattr(mod, key, value)
    return mod


def _load_real_langgraph_fleet():
    _stub_module("langchain_core")
    _stub_module(
        "langchain_core.messages",
        BaseMessage=object,
        HumanMessage=lambda *a, **k: None,
        SystemMessage=lambda *a, **k: None,
        AIMessage=lambda *a, **k: types.SimpleNamespace(content=k.get("content"), name=k.get("name")),
    )
    _stub_module("langgraph")
    _stub_module("langgraph.graph", StateGraph=object, START="START", END="END")
    _stub_module("langgraph.graph.message", add_messages=lambda *a, **k: None)

    class _FakeChatOpenAI:
        def __init__(self, *args, **kwargs):
            pass

    _stub_module("langchain_openai", ChatOpenAI=_FakeChatOpenAI)
    _stub_module("openai", RateLimitError=Exception, APIStatusError=Exception)
    _stub_module("atlassian", Jira=object)

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    module_path = os.path.join(repo_root, "agile_scripts", "langgraph_fleet.py")
    spec = importlib.util.spec_from_file_location("langgraph_fleet_real_truncation", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def real_fleet():
    from tests._real_fleet_loader import load_real_langgraph_fleet
    return load_real_langgraph_fleet("langgraph_fleet_real_anti_truncation_guard")


def _file_block(rel_path: str, content: str) -> str:
    return f"===FILE_BEGIN: {rel_path}===\n{content}===FILE_END==="


def test_rechaza_reescritura_masiva_de_archivo_grande_existente(real_fleet, tmp_path):
    """Criterio de aceptación 4: archivo simulado de 4000 líneas reescrito a 1000
    debe ser rechazado, no escrito, y reportado en `rejected`."""
    target = tmp_path / "big_migration.php"
    original_content = "\n".join(f"    ['empresa {i}', 'AR', 'tech'],," for i in range(4000))
    target.write_text(original_content)

    truncated_content = "\n".join(f"    ['empresa {i}', 'AR', 'tech'],," for i in range(1000))
    llm_response = _file_block("big_migration.php", truncated_content)

    applied, rejected = real_fleet._apply_workspace_changes(str(tmp_path), llm_response)

    assert applied == []
    assert len(rejected) == 1
    assert "big_migration.php" in rejected[0]
    # el archivo original no debe haberse tocado
    assert target.read_text() == original_content


def test_archivo_nuevo_no_activa_la_guarda_aunque_sea_chico(real_fleet, tmp_path):
    """Criterio de aceptación 5: un archivo que no existe antes nunca es 'truncamiento'."""
    llm_response = _file_block("nuevo.py", "print('hola')\n")

    applied, rejected = real_fleet._apply_workspace_changes(str(tmp_path), llm_response)

    assert applied == ["nuevo.py"]
    assert rejected == []
    assert (tmp_path / "nuevo.py").exists()


def test_archivo_existente_pequeno_no_activa_la_guarda_aunque_encoja_mucho(real_fleet, tmp_path):
    """Criterio de aceptación 5: refactors legítimos de archivos chicos no deben
    dispararla, aunque el archivo encoja drásticamente."""
    target = tmp_path / "pequeno.py"
    target.write_text("\n".join(f"x{i} = {i}" for i in range(50)))  # muy por debajo de 500 líneas / 20KB

    llm_response = _file_block("pequeno.py", "x = 1\n")

    applied, rejected = real_fleet._apply_workspace_changes(str(tmp_path), llm_response)

    assert applied == ["pequeno.py"]
    assert rejected == []
    assert target.read_text() == "x = 1\n"


def test_archivo_grande_que_no_encoge_mucho_se_escribe_normalmente(real_fleet, tmp_path):
    """Un cambio legítimo y acotado en un archivo grande (ej. 16 reemplazos
    puntuales) no debe activar la guarda."""
    lines = [f"    ['empresa {i}', 'AR', 'tech'],," for i in range(4000)]
    target = tmp_path / "big_migration.php"
    target.write_text("\n".join(lines))

    lines[0] = lines[0].replace("tech", "salud")  # 1 cambio puntual, mismo largo
    new_content = "\n".join(lines)
    llm_response = _file_block("big_migration.php", new_content)

    applied, rejected = real_fleet._apply_workspace_changes(str(tmp_path), llm_response)

    assert applied == ["big_migration.php"]
    assert rejected == []
    assert target.read_text() == new_content


def test_reviewer_node_rechaza_rapido_si_hay_archivos_rechazados(real_fleet):
    """Criterio de aceptación 3: quality_reviewer no debe poder aprobar un ciclo
    con archivos rechazados por la guarda, sin siquiera llamar al LLM."""
    state = {
        "workspace_path": "/tmp/no-usado",
        "acceptance_criteria": "cualquier cosa",
        "applied_files": ["algo_ok.py"],
        "rejected_files": ["big_migration.php: original 4000 líneas → propuesto 1000 líneas (rechazado)"],
    }

    result = real_fleet.reviewer_node(state)

    assert result["is_approved"] is False
    assert "truncamiento" in result["reviewer_feedback"].lower() or "big_migration.php" in result["reviewer_feedback"]


# ---------------------------------------------------------------------------
# Requerimiento 08: guarda de "reescritura excesiva" (>80% del archivo
# cambiado aunque el tamaño no se achique) + chequeo cruzado Angular .ts/.html
# ---------------------------------------------------------------------------

def test_rechaza_reescritura_excesiva_sin_achicamiento(real_fleet, tmp_path):
    """Caso motivador del requerimiento 08: perfil.page.html (462 líneas) pasado
    a una estructura totalmente distinta (448 líneas, tamaño similar) — el
    guard de truncamiento del req 06 no lo detecta porque no se achica, pero
    esta nueva guarda sí porque casi todo el contenido cambió."""
    original_lines = (
        ["<ion-content>"]
        + [f"  <p>línea original número {i} sin relación</p>" for i in range(200)]
        + ["</ion-content>"]
    )
    target = tmp_path / "perfil.page.html"
    target.write_text("\n".join(original_lines))

    new_lines = (
        ["<ion-header><ion-toolbar>Perfil</ion-toolbar></ion-header>"]
        + [f"  <div class=\"card-{i}\">contenido completamente distinto {i}</div>" for i in range(190)]
        + ["</ion-content>"]
    )
    llm_response = _file_block("perfil.page.html", "\n".join(new_lines))

    applied, rejected = real_fleet._apply_workspace_changes(str(tmp_path), llm_response)

    assert applied == []
    assert len(rejected) == 1
    assert "perfil.page.html" in rejected[0]
    assert target.read_text() == "\n".join(original_lines)


def test_archivo_chico_reescrito_casi_entero_no_activa_guarda_de_reescritura(real_fleet, tmp_path):
    """Caso negativo (criterio 4): un archivo de <=100 líneas puede rediseñarse
    por completo legítimamente sin disparar la guarda de reescritura excesiva."""
    target = tmp_path / "chico.py"
    target.write_text("\n".join(f"x{i} = {i}" for i in range(50)))

    llm_response = _file_block("chico.py", "def todo_nuevo():\n    return 42\n")

    applied, rejected = real_fleet._apply_workspace_changes(str(tmp_path), llm_response)

    assert applied == ["chico.py"]
    assert rejected == []


# ---------------------------------------------------------------------------
# Requerimiento 13: opt-out ALLOW_REWRITE por archivo para simplificaciones
# masivas intencionales (adaptador delgado que llama a un servicio ya extraído).
# ---------------------------------------------------------------------------

def test_allow_rewrite_permite_simplificacion_masiva_de_archivo_marcado(real_fleet, tmp_path):
    """Criterio de aceptación 1: un archivo con ALLOW_REWRITE en el
    requerimiento se reescribe >30% sin ser rechazado."""
    ruta = "src/app/api/portal/dashboard/route.ts"
    target = tmp_path / "src" / "app" / "api" / "portal" / "dashboard"
    target.mkdir(parents=True)
    (target / "route.ts").write_text(
        "\n".join(f"  const linea{i} = prismaQuery({i});" for i in range(150))
    )

    # Adaptador delgado: ~150 líneas de lógica inline → ~6 líneas que llaman al servicio.
    adaptador = (
        "export async function GET() {\n"
        "  const data = await getStudentDashboard();\n"
        "  return Response.json(data);\n"
        "}\n"
    )
    criteria = (
        f"Migra la ruta a un adaptador delgado que llame a getStudentDashboard.\n"
        f"ALLOW_REWRITE: {ruta}"
    )
    llm_response = _file_block(ruta, adaptador)

    applied, rejected = real_fleet._apply_workspace_changes(str(tmp_path), llm_response, criteria=criteria)

    assert applied == [ruta]
    assert rejected == []


def test_allow_rewrite_es_por_archivo_otros_siguen_protegidos(real_fleet, tmp_path):
    """Criterio de aceptación 3: en un mismo ciclo, el archivo con la marca se
    aprueba y otro sin la marca (>30% de cambio) se rechaza — independientes."""
    marcado = "src/app/api/mobile/dashboard/route.ts"
    sin_marca = "src/server/payments/payment-service.ts"
    for ruta in (marcado, sin_marca):
        p = tmp_path / ruta
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("\n".join(f"  const linea{i} = compute({i});" for i in range(150)))

    criteria = (
        f"Migra el dashboard a adaptador delgado y ajusta el payment-service.\n"
        f"ALLOW_REWRITE: {marcado}"
    )
    thin = "export async function GET() { return Response.json(await getCoachDashboard()); }\n"
    massive_rewrite = "\n".join(f"  const nuevo{i} = otraCosa({i});" for i in range(140))
    llm_response = _file_block(marcado, thin) + "\n" + _file_block(sin_marca, massive_rewrite)

    applied, rejected = real_fleet._apply_workspace_changes(str(tmp_path), llm_response, criteria=criteria)

    assert applied == [marcado]
    assert len(rejected) == 1
    assert sin_marca in rejected[0]


def test_sin_allow_rewrite_comportamiento_del_req_12_intacto(real_fleet, tmp_path):
    """Criterio de aceptación 2: sin la marca, un archivo >30% sigue rechazado."""
    ruta = "src/app/api/portal/dashboard/route.ts"
    p = tmp_path / ruta
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(f"  const linea{i} = prismaQuery({i});" for i in range(150)))

    thin = "export async function GET() { return Response.json(await getStudentDashboard()); }\n"
    llm_response = _file_block(ruta, thin)
    # criteria SIN ALLOW_REWRITE
    criteria = "Migra la ruta a un adaptador delgado."

    applied, rejected = real_fleet._apply_workspace_changes(str(tmp_path), llm_response, criteria=criteria)

    assert applied == []
    assert len(rejected) == 1
    assert ruta in rejected[0]


def test_rechaza_html_angular_que_referencia_metodo_inexistente_en_ts(real_fleet, tmp_path):
    """Criterio de aceptación 2 y 3: el .html referencia un método nuevo que el
    agente nunca agregó al .ts correspondiente — debe rechazarse."""
    ts_content = (
        "export class PerfilPage {\n"
        "  canRecordar(rec: any): boolean { return true; }\n"
        "}\n"
    )
    (tmp_path / "perfil.page.ts").write_text(ts_content)

    html_content = (
        '<div class="card" *ngIf="rec.estado === \'pendiente\' && !canRecordar(rec)">\n'
        "  <span>{{ diasParaRecordar(rec) }} {{ diasParaRecordarSingular(rec) ? 'día' : 'días' }}</span>\n"
        "</div>\n"
    )
    llm_response = _file_block("perfil.page.html", html_content)

    applied, rejected = real_fleet._apply_workspace_changes(str(tmp_path), llm_response)

    assert applied == []
    assert len(rejected) == 1
    assert "diasParaRecordar" in rejected[0]
    assert not (tmp_path / "perfil.page.html").exists()


# ---------------------------------------------------------------------------
# Requerimiento 12: el umbral de la guarda de reescritura excesiva (req 08)
# era >80% de cambio — muy alto. Una reescritura real de ~65% (con
# regresiones reales: se perdió un límite de paginación, includes de Prisma,
# el tipo Zod compartido y un helper de autorización) pasó sin detectarse.
# El umbral bajó a >30%, alineado con el resto de las guardas.
# ---------------------------------------------------------------------------

def test_rechaza_reescritura_de_65_por_ciento_que_antes_pasaba_el_umbral_viejo(real_fleet, tmp_path):
    """Caso motivador del requerimiento 12: payment-service.ts, ~65% de cambio,
    por debajo del umbral anterior (80%) pero por encima del actual (30%)."""
    original_lines = [f"line_{i} original" for i in range(177)]
    target = tmp_path / "payment-service.ts"
    target.write_text("\n".join(original_lines))

    # Mantiene los primeros 61 (como el caso real: 116 líneas eliminadas de 177),
    # agrega 50 líneas nuevas — ~65% de cambio real.
    new_lines = original_lines[:61] + [f"newline_{i} reescrita" for i in range(50)]
    llm_response = _file_block("payment-service.ts", "\n".join(new_lines))

    applied, rejected = real_fleet._apply_workspace_changes(str(tmp_path), llm_response)

    assert applied == []
    assert len(rejected) == 1
    assert "payment-service.ts" in rejected[0]
    assert target.read_text() == "\n".join(original_lines)


def test_ambos_archivos_del_mismo_ciclo_se_rechazan_si_ambos_superan_el_umbral(real_fleet, tmp_path):
    """Criterio de aceptación 2: un ciclo que escribe 2 archivos, ambos con
    >30% de cambio, debe rechazar AMBOS, no solo el primero."""
    original_a = [f"a_line_{i}" for i in range(150)]
    original_b = [f"b_line_{i}" for i in range(150)]
    (tmp_path / "archivo_a.ts").write_text("\n".join(original_a))
    (tmp_path / "archivo_b.ts").write_text("\n".join(original_b))

    new_a = original_a[:50] + [f"a_new_{i}" for i in range(80)]
    new_b = original_b[:50] + [f"b_new_{i}" for i in range(80)]
    llm_response = (
        _file_block("archivo_a.ts", "\n".join(new_a))
        + "\n"
        + _file_block("archivo_b.ts", "\n".join(new_b))
    )

    applied, rejected = real_fleet._apply_workspace_changes(str(tmp_path), llm_response)

    assert applied == []
    assert len(rejected) == 2
    assert any("archivo_a.ts" in r for r in rejected)
    assert any("archivo_b.ts" in r for r in rejected)


def test_no_rechaza_html_angular_cuando_el_metodo_existe_en_el_ts(real_fleet, tmp_path):
    """Caso negativo: si el método referenciado sí existe en el .ts (ya en
    disco), el .html no debe rechazarse."""
    ts_content = (
        "export class PerfilPage {\n"
        "  canRecordar(rec: any): boolean { return true; }\n"
        "  diasParaRecordar(rec: any): number { return 3; }\n"
        "}\n"
    )
    (tmp_path / "perfil.page.ts").write_text(ts_content)

    html_content = '<span *ngIf="canRecordar(rec)">{{ diasParaRecordar(rec) }}</span>\n'
    llm_response = _file_block("perfil.page.html", html_content)

    applied, rejected = real_fleet._apply_workspace_changes(str(tmp_path), llm_response)

    assert applied == ["perfil.page.html"]
    assert rejected == []
