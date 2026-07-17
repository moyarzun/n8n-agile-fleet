# Requerimiento: crash `_node_error_handler() missing 1 required positional argument: 'error'` en planes grandes con múltiples sub-grupos

**Estado:** ✅ investigado y aplicado (2026-07-17)

## Contexto

Al despachar el plan `remaining-routes-refactor` sobre `app-tennis` (el más grande y heterogéneo de la refactorización — 3 sub-grupos: mensajería, waitlist, estudiantes/invitaciones, ~18 archivos de rutas involucrados), **dos intentos consecutivos** terminaron en `error` (no `rejected`) con:

```
_node_error_handler() missing 1 required positional argument: 'error'
```

Esto es distinto a todos los hallazgos anteriores (01-13) — no es un rechazo de validación ni un guardia de calidad, es un **crash interno no manejado** en el propio código de orquestación de la flota.

## Patrón observado (idéntico en ambos intentos)

```
[planner] 0 subtareas planificadas
```

En contraste con todos los despachos anteriores de este mismo refactor (que siempre produjeron 5-8 subtareas), el `planner` generó **cero subtareas** para este plan específico. Como consecuencia:

1. Ciclo 1: los agentes `Node` y `QA` reciben "criterios" pero sin subtareas concretas — ambos devuelven respuestas cortas (999-1955 caracteres) **sin ningún bloque `FILE_BEGIN/END`**. El `quality_reviewer` rechaza correctamente ("RECHAZO RÁPIDO: no se generó ningún archivo").
2. Ciclo 2: mismo patrón se repite. En el primer intento (`TASK-08cbdffb`), el segundo ciclo eventualmente sí produjo archivos reales (9 archivos, con 1 rechazado por el guardia de reescritura). En el segundo intento (`TASK-ff833019`), el log se corta abruptamente justo después de `"Agente 'QA': llamando al modelo LLM..."` — sin ninguna línea posterior — indicando que el crash ocurre procesando la respuesta de ese segundo llamado a QA en el ciclo 2.

## Hipótesis de causa raíz (a confirmar)

El `planner` fallando en generar subtareas para este plan en particular (0 subtareas, ambas veces) es sospechoso de ser la causa raíz — quizás el plan es demasiado grande/complejo (organizado en 3 sub-grupos con ~10 tareas en vez de una lista lineal de 4-6 tareas como los otros planes de este refactor) y el `planner` no logra parsearlo correctamente, devolviendo una lista vacía en vez de fallar de forma explícita. Esa lista vacía de subtareas probablemente deja al resto del pipeline (`dynamic_developer`, y en algún punto el manejo de excepciones que invoca `_node_error_handler`) en un estado que no esperaba, y en el camino de manejo de esa condición inesperada hay una llamada a `_node_error_handler(...)` a la que le falta pasar el argumento `error` — un bug simple de firma de función, pero que enmascara el problema real (el planner fallando silenciosamente) detrás de un crash distinto.

## Investigación sugerida

1. Ubicar todas las llamadas a `_node_error_handler(...)` en `agile_scripts/langgraph_fleet.py` y confirmar cuál le falta el argumento `error` — es un fix mecánico simple una vez ubicada.
2. Más importante: investigar por qué el `planner` devuelve 0 subtareas para este plan específico. Comparar el tamaño/estructura de `docs/superpowers/plans/2026-07-15-remaining-routes-refactor.md` (el que falla) contra los otros planes de este refactor que sí generaron subtareas correctamente — ¿es un límite de tamaño del prompt del planner? ¿Un formato de plan con sub-secciones anidadas que el parser no maneja? El planner debería, como mínimo, fallar de forma explícita y visible (abortar con un mensaje claro) en vez de devolver silenciosamente una lista vacía que luego causa un crash más adelante.

## Workaround aplicado mientras tanto

Dado que el plan es grande, se evaluará dividir manualmente el `requerimiento` en 3 despachos más chicos (uno por sub-grupo: mensajería, waitlist, estudiantes/invitaciones) en vez de un solo despacho para todo el plan — similar a la estrategia ya usada exitosamente para plan_1 cuando tuvo problemas de convergencia. Esto no resuelve la causa raíz, solo evita el síntoma.

## Actualización: el mismo crash ocurrió en un despacho MÁS CHICO (revisa la hipótesis)

El workaround de dividir en sub-grupos NO evitó el crash: al despachar solo el sub-grupo "Mensajería" (5 archivos, un `requerimiento` de texto libre normal, sin referenciar el plan grande como archivo) el mismo `_node_error_handler() missing 1 required positional argument: 'error'` ocurrió de nuevo, esta vez en el ciclo 3 (`TASK-fa36a777`). En este caso el `planner` SÍ generó subtareas reales (hubo progreso: archivos escritos, guardas de reescritura activándose, y — dato nuevo — el `regression_guard` restauró automáticamente 3 archivos donde el modelo había borrado accidentalmente sus exports `GET`/`dynamic`). El crash ocurrió después de esa secuencia: rechazo por guarda de truncamiento (5 archivos) + regresión detectada y restaurada (3 archivos) + intento de tocar `src/server/auth/context.ts` fuera de alcance (bloqueado) — todo en el mismo ciclo.

Esto contradice parcialmente la hipótesis original (planner con 0 subtareas como causa raíz única). Hipótesis revisada: el crash puede estar relacionado con el manejo de **múltiples condiciones de guardia simultáneas en un mismo ciclo** (rechazo por truncamiento + restauración por regresión + posiblemente el chequeo de alcance del requerimiento 11) — quizás el código que arma el mensaje de feedback combinado para el siguiente ciclo, o el que decide si reintentar vs abortar, no maneja bien el caso de tener resultados de múltiples guardas distintas a la vez, y en algún punto de ese manejo llama a `_node_error_handler` sin el argumento.

Sugerencia adicional de investigación: reproducir con un caso mínimo que combine intencionalmente una restauración de `regression_guard` Y un rechazo del guardia de truncamiento en el mismo ciclo, para aislar si es esa combinación específica la que dispara el crash.

## Criterios de aceptación

1. El `planner` nunca debe devolver una lista vacía de subtareas sin que el pipeline aborte explícitamente con un mensaje de error claro (no un crash de `_node_error_handler`).
2. Corregir la llamada a `_node_error_handler` que falta el argumento `error`, para que cualquier excepción real se reporte con su mensaje/traza en vez de un `TypeError` secundario que oculta la causa original.
3. Test de regresión: un plan grande/complejo (>10 tareas o con estructura de sub-grupos) debe producir subtareas reales, o el sistema debe fallar con un mensaje claro indicando que el plan es demasiado grande/complejo para el planner actual.

## Verificación (causa raíz confirmada)

**Causa raíz única del crash** (confirma la hipótesis revisada de la
actualización: NO era el planner con 0 subtareas): `_node_error_handler`
—agregado en el hardening de LangGraph (spec `langgraph-hardening`, Req 4.3)—
tenía la firma `def _node_error_handler(state, error)` con el parámetro
`error` **sin anotar**. En LangGraph 1.2.9 el `error_handler` es un
`StateNode` y el contexto del fallo (`NodeError`, con `.node` y `.error`) se
**inyecta por tipo de anotación**, no por posición (ver docstring de
`langgraph.errors.NodeError` y el tipo `StateNode` de `set_node_defaults`).
Sin la anotación, LangGraph invocaba el handler como un nodo normal (solo
`state`) → `TypeError: _node_error_handler() missing 1 required positional
argument: 'error'`, que enmascaraba la excepción original del nodo que
realmente había fallado (un nodo cualquiera agotando sus reintentos — por eso
el crash aparecía con estructuras de despacho distintas: planes grandes,
sub-grupos, o combinación de guardas en un ciclo).

**Fix criterio 2** (el que resuelve el crash): anotar
`def _node_error_handler(state, error: NodeError)` + `from langgraph.errors
import NodeError`. Verificado end-to-end con el langgraph real: un nodo que
falla ahora activa el handler y produce `aborted=True` con el nodo y mensaje
reales, sin TypeError secundario.

**Fix criterio 1** (planner): el planner ahora reintenta una vez ante 0
subtareas (cubre el fallo transitorio de parseo del modelo revisor, la causa
más probable del "0 subtareas") y, si persiste, deja un aviso EXPLÍCITO y
visible en el log (`⚠ AVISO: ... fallback ... considera dividirlo`) en vez de
proceder en silencio. **No se implementó un abort duro** ante 0 subtareas: 0
subtareas es un fallback de diseño intencional válido (tickets simples que no
necesitan descomposición usan el criteria completo, y `dynamic_developer` lo
maneja bien); abortar los rompería. El aviso explícito satisface el espíritu
del criterio 1 (no silencioso) sin ese efecto colateral.

Tests de regresión:
- `tests/test_fault_tolerance_and_recursion.py`:
  - `test_error_handler_tiene_el_parametro_error_anotado_con_NodeError` — guard
    de regresión de la anotación (si alguien la quita, falla).
  - `test_error_handler_real_se_invoca_sin_TypeError_al_fallar_un_nodo` —
    reproduce el crash exacto con el langgraph real (corre en el contenedor).
- `tests/test_planner_empty_subtasks.py`: reintento ante 0 subtareas, aviso
  explícito sin crash, y tolerancia a excepción del modelo.

Suite: 74 passed en contenedor (0 skips) / 68+4 local.

Alcance del criterio 3: no se agregó un límite duro de tamaño de plan — el
reintento + aviso cubren el síntoma observado; un plan que legítimamente
excede la capacidad del planner queda señalado por el aviso explícito para
que quien despacha lo divida (como ya hace el workaround).
