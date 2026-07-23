"""
Regresión para el punto 4 (prioridad media) del requerimiento 18: validation_gate
no cubría tickets 100% dentro de un subdirectorio con su propio package.json
(ej. mobile/) — corría tsc/vitest de la raíz, que ni siquiera incluye ese
código, dando un "✓ validado" sin cobertura real (ver
requerimientos/18-dynamic-developer-alucina-resultado-de-comandos-npm-no-los-ejecuta.md).
"""
import pytest


@pytest.fixture(scope="module")
def real_fleet():
    from tests._real_fleet_loader import load_real_langgraph_fleet
    return load_real_langgraph_fleet("langgraph_fleet_real_validation_gate_subproject")


# ---------------------------------------------------------------------------
# _find_subproject_root
# ---------------------------------------------------------------------------

def test_detecta_subproyecto_cuando_100pct_bajo_un_dir_con_package_json(real_fleet, tmp_path):
    (tmp_path / "mobile").mkdir()
    (tmp_path / "mobile" / "package.json").write_text("{}\n")

    subproject = real_fleet._find_subproject_root(
        ["mobile/lib/waitlist.test.ts", "mobile/jest.config.js"], str(tmp_path),
    )
    assert subproject == "mobile"


def test_no_detecta_si_hay_un_archivo_en_la_raiz(real_fleet, tmp_path):
    (tmp_path / "mobile").mkdir()
    (tmp_path / "mobile" / "package.json").write_text("{}\n")

    subproject = real_fleet._find_subproject_root(
        ["mobile/jest.config.js", "src/lib/foo.ts"], str(tmp_path),
    )
    assert subproject is None


def test_no_detecta_si_hay_archivos_en_mas_de_un_subdir(real_fleet, tmp_path):
    (tmp_path / "mobile").mkdir()
    (tmp_path / "mobile" / "package.json").write_text("{}\n")
    (tmp_path / "packages").mkdir()
    (tmp_path / "packages" / "package.json").write_text("{}\n")

    subproject = real_fleet._find_subproject_root(
        ["mobile/jest.config.js", "packages/shared/index.ts"], str(tmp_path),
    )
    assert subproject is None


def test_no_detecta_si_el_subdir_no_tiene_package_json_propio(real_fleet, tmp_path):
    (tmp_path / "docs").mkdir()

    subproject = real_fleet._find_subproject_root(["docs/guide.md"], str(tmp_path))
    assert subproject is None


def test_lista_vacia_no_detecta_nada(real_fleet, tmp_path):
    assert real_fleet._find_subproject_root([], str(tmp_path)) is None


# ---------------------------------------------------------------------------
# _validate_workspace enruta tsc/vitest al subproyecto
# ---------------------------------------------------------------------------

TSC_OK = ""
VITEST_OK = """
 RUN  v1.0.0

 Test Files  1 passed (1)
      Tests  4 passed (4)
"""


def _make_recording_run(calls: list, vitest_output: str = VITEST_OK, vitest_rc: int = 0):
    def fake_run(cmd, cwd, timeout=120):
        calls.append({"cmd": cmd, "cwd": cwd})
        if cmd[:1] == ["npx"]:
            return 0, "npx/10.0.0"
        if any("tsc" in part for part in cmd):
            return 0, TSC_OK
        if any("vitest" in part for part in cmd):
            return vitest_rc, vitest_output
        return 0, ""
    return fake_run


def test_tsc_y_vitest_corren_con_cwd_en_el_subproyecto(real_fleet, monkeypatch, tmp_path):
    """Caso motivador: un ticket 100% mobile/ debe correr tsc/vitest DENTRO de
    mobile/ (donde vive su propio package.json/tsconfig.json), no en la raíz
    del workspace, que ni siquiera incluye ese código."""
    mobile = tmp_path / "mobile"
    mobile.mkdir()
    (mobile / "package.json").write_text("{}\n")
    (mobile / "vitest.config.ts").write_text("export default {}\n")

    calls = []
    monkeypatch.setattr(real_fleet, "_run", _make_recording_run(calls))

    passed, report = real_fleet._validate_workspace(
        str(tmp_path),
        ["mobile/lib/waitlist.test.ts"],
        "node",
    )

    assert passed is True
    assert "SUBPROYECTO DETECTADO" in report
    assert "mobile/" in report
    tsc_calls = [c for c in calls if any("tsc" in p for p in c["cmd"])]
    vitest_calls = [c for c in calls if any("vitest" in p for p in c["cmd"])]
    assert tsc_calls and all(c["cwd"] == str(mobile) for c in tsc_calls)
    assert vitest_calls and all(c["cwd"] == str(mobile) for c in vitest_calls)


def test_sin_subproyecto_tsc_y_vitest_siguen_corriendo_en_la_raiz(real_fleet, monkeypatch, tmp_path):
    """No regression: el caso normal (archivos en src/, sin subproyecto
    aislado) sigue corriendo tsc/vitest en la raíz del workspace, como antes."""
    (tmp_path / "vitest.config.ts").write_text("export default {}\n")

    calls = []
    monkeypatch.setattr(real_fleet, "_run", _make_recording_run(calls))

    passed, report = real_fleet._validate_workspace(
        str(tmp_path),
        ["src/lib/foo.test.ts"],
        "node",
    )

    assert passed is True
    assert "SUBPROYECTO DETECTADO" not in report
    tsc_calls = [c for c in calls if any("tsc" in p for p in c["cmd"])]
    vitest_calls = [c for c in calls if any("vitest" in p for p in c["cmd"])]
    assert tsc_calls and all(c["cwd"] == str(tmp_path) for c in tsc_calls)
    assert vitest_calls and all(c["cwd"] == str(tmp_path) for c in vitest_calls)


def test_baseline_de_la_raiz_no_se_usa_para_el_subproyecto(real_fleet, monkeypatch, tmp_path):
    """El baseline de vitest se calcula siempre en la raíz, antes de saber si
    el ticket cae en un subproyecto — no es comparable contra los fallos de
    la suite de mobile/. Con subproyecto detectado, cualquier fallo debe
    bloquear (modo estricto), aunque se haya pasado un baseline no vacío."""
    mobile = tmp_path / "mobile"
    mobile.mkdir()
    (mobile / "package.json").write_text("{}\n")
    (mobile / "vitest.config.ts").write_text("export default {}\n")

    vitest_failure_output = """
 RUN  v1.0.0

 FAIL  mobile/lib/waitlist.test.ts > algo > falla
AssertionError: nope

 Test Files  1 failed (1)
      Tests  1 failed (1)
"""
    calls = []
    monkeypatch.setattr(
        real_fleet, "_run",
        _make_recording_run(calls, vitest_output=vitest_failure_output, vitest_rc=1),
    )

    # baseline "de la raíz" que, por coincidencia de texto, ya marcaría este
    # mismo test como preexistente si se usara mal (no debería usarse).
    fake_root_baseline = {"mobile/lib/waitlist.test.ts > algo > falla"}

    passed, report = real_fleet._validate_workspace(
        str(tmp_path),
        ["mobile/lib/waitlist.test.ts"],
        "node",
        baseline_failing_tests=fake_root_baseline,
    )

    assert passed is False
