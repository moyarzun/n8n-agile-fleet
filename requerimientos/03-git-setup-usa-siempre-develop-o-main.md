# Requerimiento: `git_setup_node` debe usar la rama actualmente checked out como base, no siempre develop/main

**Estado:** ✅ aplicado (2026-07-16)

## Contexto

Detectado el 2026-07-16 al despachar un requerimiento sobre `app-tennis` sin ticket de Jira (modo libre, `resolver_requerimiento`). El usuario tenía la rama `refactor/modular-dry` checked out en el workspace montado (`/vaults/sdelvillar/tennis-app/app-tennis`) — esa rama contiene 9 commits nuevos: un spec de diseño y 8 planes de implementación detallados (`docs/superpowers/plans/*.md`) que el requerimiento le pedía a la flota que leyera y ejecutara.

## Problema

`git_setup_node` (en `agile_scripts/langgraph_fleet.py`, función completa alrededor de la línea 856) resuelve la rama base así (línea ~887 en adelante):

```python
    # Rama base: develop si existe (GitFlow), si no la rama por defecto del repo.
    rc, out = _git(["rev-parse", "--verify", "develop"], workspace)
    if rc == 0:
        base = "develop"
    else:
        rc, out = _git(["symbolic-ref", "refs/remotes/origin/HEAD"], workspace)
        base = out.strip().split("/")[-1] if rc == 0 and out.strip() else "main"
```

Esto **nunca considera qué rama está actualmente checked out en `workspace`** — siempre resuelve a `develop` (si existe como rama local) o al branch por defecto del remoto (típicamente `main`). El resultado: el `git worktree add` crea el nuevo worktree del ticket partiendo de `main`, **descartando por completo** cualquier trabajo en la rama que el usuario tenía activa (en este caso, los 9 commits de `refactor/modular-dry` con los planes que el requerimiento necesitaba leer).

Consecuencia observada: el agente `Node` de la flota nunca tuvo acceso real al archivo `docs/superpowers/plans/2026-07-15-payments-webhooks-refactor.md` (no existía en el worktree, porque partía de `main`). En vez de fallar de forma clara, el LLM **fabricó un archivo nuevo con el mismo nombre** reconstruyendo un resumen a partir del texto libre del `requerimiento`, con nombres de archivo y estructura de tests distintos a los especificados — 6 ciclos después, terminó en `REQUIERE REVISIÓN` con 16 tests fallando, habiendo gastado ~1h40 de cómputo sin posibilidad real de converger, porque nunca tuvo el contenido real que se le pidió implementar.

## Fix propuesto

Reemplazar el bloque de resolución de `base` (línea ~887-892) por:

```python
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
```

No requiere cambios en el resto de la función — la lógica de `fetch_rc`/`base_ref` que sigue (intenta `origin/<base>`, cae a `<base>` local si el fetch falla) ya funciona igual de bien con cualquier nombre de rama, no solo `develop`/`main`.

## Criterios de aceptación

1. Con el workspace en una rama feature local sin push a ningún remoto (ej. `refactor/modular-dry`, nunca pusheada), un nuevo despacho debe crear su worktree partiendo de esa rama — `git merge-base <rama-feature> fleet/TASK-...` debe ser igual al HEAD de la rama feature en el momento del despacho, no a un ancestro más antiguo compartido con `main`.
2. Con HEAD detached en el workspace, debe seguir cayendo al comportamiento anterior (develop → origin/HEAD → "main") sin lanzar excepción.
3. Con el workspace en `main` (caso más común, sin rama feature activa), el comportamiento no debe cambiar — `base` sigue resolviendo a `main` como antes (porque `symbolic-ref --short HEAD` devolvería literalmente `main`).
4. Test de regresión: agregar un caso a la suite de tests de `git_setup_node` (ver `tests/` en la raíz de este proyecto) que verifique el punto 1 con un repo de prueba desechable (crear rama, commitear un archivo, checkoutear esa rama, invocar `git_setup_node`, confirmar que el archivo existe en el worktree resultante).

## Cómo verificarlo manualmente tras aplicar

```bash
cd /Users/moyarzun/vaults/sdelvillar/tennis-app/app-tennis
git checkout refactor/modular-dry  # o cualquier rama feature local sin push
# despachar un requerimiento de prueba vía resolver_requerimiento
# luego, desde el repo:
git merge-base refactor/modular-dry fleet/TASK-<nuevo>
git rev-parse refactor/modular-dry
# ambos deben coincidir (o el merge-base debe ser un ancestro MUY reciente,
# no el commit donde refactor/modular-dry divergió de main)
```

## Verificación

Fix aplicado en `agile_scripts/langgraph_fleet.py` (resolución de `base` vía
`git symbolic-ref --short HEAD`, con fallback a develop/origin-HEAD/main si
HEAD está detached).

Test de regresión agregado en `tests/test_git_setup_node.py` (criterio de
aceptación #4), cargando el módulo real por ruta de archivo para no chocar
con el stub global de `langgraph_fleet` que usa `test_fleet_api.py`. Cubre
los 3 criterios de aceptación:

```
tests/test_git_setup_node.py::test_usa_la_rama_feature_checked_out_como_base PASSED
tests/test_git_setup_node.py::test_head_detached_cae_a_comportamiento_anterior PASSED
tests/test_git_setup_node.py::test_en_main_sin_rama_feature_sigue_resolviendo_a_main PASSED
```

Nota: `tests/conftest.py` tenía un bug preexistente y no relacionado (el mock
de `langgraph_fleet` no exponía `stop_gracefully`/`set_log_callback`, que
`fleet_api.py` ya importa) que rompía la colección de toda la suite —
corregido de paso para poder correr los tests.
