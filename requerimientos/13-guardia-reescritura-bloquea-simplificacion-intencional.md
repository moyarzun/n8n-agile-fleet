# Requerimiento: implementar la excepción para "simplificación masiva intencional" ya sugerida en el requerimiento 12

**Estado:** ✅ aplicado (2026-07-17)

## Contexto

El requerimiento 12 (guardia de reescritura excesiva) ya identificó y documentó este caso como una nota no bloqueante, pero se volvió bloqueante en la práctica: en el plan `dashboards-refactor` sobre `app-tennis`, la migración de 3 rutas legacy (`src/app/api/portal/dashboard/route.ts`, `src/app/api/mobile/student/dashboard/route.ts`, `src/app/api/mobile/dashboard/route.ts`) a "adaptadores delgados" que llaman a servicios ya existentes fue rechazada **dos veces consecutivas** por el guardia de reescritura excesiva (ahora en 30% tras el fix del requerimiento 12), con el mismo mensaje ambas veces:

```
src/app/api/portal/dashboard/route.ts: ~90% del archivo cambiado — rechazado
src/app/api/mobile/student/dashboard/route.ts: ~93% del archivo cambiado — rechazado
src/app/api/mobile/dashboard/route.ts: ~90% del archivo cambiado — rechazado
```

Este es exactamente el caso previsto en la sección "Sobre el falso positivo en las 3 rutas de dashboard" del requerimiento 12: el plan pide explícitamente reemplazar ~150 líneas de lógica Prisma inline por ~20 líneas que llaman a un servicio ya extraído y testeado (`getStudentDashboard`/`getCoachDashboard`) — un 90%+ de cambio es el resultado CORRECTO y esperado, no una reescritura indebida. El resto del plan (Tasks 1-4 y 6, en archivos distintos) se completó sin problemas en 2 despachos separados; estas 3 rutas son el único bloqueante restante, y ya se intentó dos veces sin éxito.

## Fix pedido (ya especificado en el requerimiento 12, sección de nota — formalizándolo acá como bloqueante)

Cuando el `requerimiento`/plan referenciado contiene lenguaje explícito indicando una simplificación masiva intencional para un archivo específico (frases como "adaptador delgado", "reemplaza toda la lógica inline por una llamada al servicio", "reduce a ~N líneas"), el guardia de reescritura excesiva debe:
1. Reconocer esas frases (asociadas al archivo que mencionan) y relajar o saltar el umbral de 30% para ese archivo específico, O
2. Como alternativa más simple y menos frágil (no depender de parseo de lenguaje natural): permitir que el `requerimiento` incluya una marca explícita tipo `ALLOW_REWRITE: <ruta/archivo>` que el `codebase_reader`/`_apply_workspace_changes` reconozcan como opt-out explícito del guardia para ese archivo puntual, dejando la responsabilidad de la decisión en quien redacta el requerimiento (que ya revisó el plan y sabe que la reescritura es intencional).

Prefiero la opción 2 (marca explícita) sobre la 1 (heurística de lenguaje) — es determinística, no depende de que el modelo interprete bien una frase, y es fácil de auditar (la marca queda en el texto del requerimiento, visible en el log).

## Criterios de aceptación

1. Un `requerimiento` que incluye `ALLOW_REWRITE: src/app/api/portal/dashboard/route.ts` (o el mecanismo que se implemente) permite que ESE archivo específico se reescriba >30% sin ser rechazado por el guardia, mientras que otros archivos del mismo ciclo sin esa marca siguen protegidos normalmente.
2. Sin la marca, el comportamiento no cambia respecto al fix del requerimiento 12 (cualquier archivo >30% sigue rechazado).
3. Test de regresión: un ciclo que escribe 2 archivos, uno con la marca de opt-out y >30% de cambio (debe aprobarse) y otro sin la marca y >30% de cambio (debe rechazarse) — confirmar que el guardia trata a cada uno correctamente de forma independiente.

## Una vez aplicado

Reintentar el despacho de las 3 rutas de `dashboards-refactor` incluyendo `ALLOW_REWRITE: <ruta>` para cada una de las 3 en el `requerimiento`.

## Verificación

Implementada la **opción 2** (marca explícita, la preferida): nueva función
`_find_allow_rewrite_files(criteria)` en `agile_scripts/langgraph_fleet.py`
que extrae las rutas de las líneas `ALLOW_REWRITE: <ruta>` del texto del
requerimiento vía regex. En `_apply_workspace_changes`, un archivo listado
así salta **ambas** guardas basadas en tamaño (truncamiento del req 06 y
reescritura excesiva del req 08/12), quedando el opt-out registrado en el log
(`logger.info`). Las demás guardas siguen vigentes para ese archivo (alcance
del req 11, chequeo cruzado Angular) y los archivos sin la marca no se ven
afectados.

Nota: como `ALLOW_REWRITE: <ruta>` incluye la ruta en el texto, el
`codebase_reader` la detecta y lee el archivo original igual que a cualquier
otro archivo referenciado (inyecta el contenido para que el agente vea qué
está simplificando).

Tests de regresión en `tests/test_anti_truncation_guard.py` (los 3 criterios
de aceptación):

```
test_allow_rewrite_permite_simplificacion_masiva_de_archivo_marcado PASSED
test_allow_rewrite_es_por_archivo_otros_siguen_protegidos PASSED
test_sin_allow_rewrite_comportamiento_del_req_12_intacto PASSED
```

El segundo cubre el criterio 3 explícitamente: mismo ciclo, un archivo con la
marca (>30% cambiado, se aprueba) y otro sin la marca (>30% cambiado, se
rechaza) — tratados de forma independiente.
