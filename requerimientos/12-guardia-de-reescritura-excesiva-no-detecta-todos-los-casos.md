# Requerimiento: el guardia de "reescritura excesiva" no detectó una reescritura real con regresiones en `payment-service.ts`

**Estado:** ✅ investigado y aplicado (2026-07-16)

## Contexto

Al despachar el plan `dashboards-refactor` sobre `app-tennis` (`TASK-50a00bba`), el job terminó en `rejected` en el ciclo 6 con este mensaje — confirmando que SÍ existe un guardia de "reescritura excesiva" (no documentado en los requerimientos 01-11, es la primera vez que se observa):

```
Uno o más archivos fueron rechazados porque el contenido generado perdió más del 30% de sus
líneas/bytes respecto al original (posible truncamiento o reescritura no solicitada):

src/app/api/portal/dashboard/route.ts: ~90% del archivo cambiado — rechazado
src/app/api/mobile/student/dashboard/route.ts: ~93% del archivo cambiado — rechazado
src/app/api/mobile/dashboard/route.ts: ~90% del archivo cambiado — rechazado
src/server/classes/class-service.ts: ~86% del archivo cambiado — rechazado
```

Este guardia es bueno y necesario — de hecho para 3 de esos 4 archivos el rechazo es un FALSO POSITIVO benigno (el plan pedía explícitamente convertir las 3 rutas de dashboard en adaptadores delgados de ~20 líneas, reemplazando ~150 líneas de lógica inline — un ~90% de cambio ahí es el resultado CORRECTO y esperado, no una reescritura indebida). Pero el caso de `class-service.ts` si era indebido (el plan solo pedía agregar una línea `revalidateTag(...)` a 5 funciones existentes, no reescribir el 86% del archivo) — y el guardia lo bloqueó correctamente.

## Problema encontrado: el guardia no es consistente

`src/server/payments/payment-service.ts` tenía el MISMO tipo de instrucción que `class-service.ts` en este ticket (agregar `revalidateTag(...)` a 2 funciones existentes, sin más cambios) — y el modelo lo reescribió de forma similarmente indebida, PERO el guardia no lo rechazó ni lo mencionó en el resumen final. El archivo SÍ quedó commiteado en la rama resultante, con una reescritura real de ~65% (111 líneas nuevas sobre 177 originales; 116 líneas eliminadas, 50 agregadas) que introdujo regresiones reales:

- **Se eliminó `take: 200`** de `listPayments` (el límite de paginación agregado en un plan anterior para evitar queries sin cota) — reintroduce el problema de performance que ya se había corregido.
- **Se eliminó `include: { createdByUser: true }` y `plan: true`** del include de `studentAcademy` — pérdida silenciosa de datos para cualquier consumidor de `listPayments` que dependa de esos campos.
- **Se rompió el contrato de tipos con `payment-schema.ts`**: `createManualPaymentForAcademy` dejó de usar el tipo `ManualPaymentInput` (derivado del schema Zod `manualPaymentSchema`) y lo reemplazó por un tipo anónimo inline — desconecta la validación Zod del tipo de la función.
- **Mensajes de error traducidos de español a inglés** ("Alumno no encontrado" → "Student not found in this academy"), inconsistente con el resto del código en español.
- **`assertCanManagePayments` dejó de usar el helper compartido `assertRole`** (de `src/server/auth/context.ts`), reimplementando el chequeo de rol inline — pierde la única fuente de verdad de esa lógica.

## Por qué esto es más grave que los hallazgos anteriores

A diferencia del requerimiento 11 (donde el mecanismo de protección de alcance ya existía y funcionó, evitando el merge), acá el guardia de reescritura excesiva **existe pero no cubrió este caso** — un archivo con ~65% de cambio (por encima del 30% que sí disparó el rechazo en los otros 4 archivos) pasó sin ser detectado ni mencionado. Esto sugiere que el guardia no se aplica de forma uniforme a todos los archivos modificados en un ciclo, o que hay alguna condición (tamaño del archivo, tipo de cambio, orden de escritura) que lo hace saltarse algunos casos.

## Investigación sugerida

1. Confirmar en qué función/nodo vive el guardia de reescritura excesiva (no documentado hasta ahora en los requerimientos 01-11 — parece haberse agregado o activado en algún punto sin que quedara registrado). Ubicarlo en `agile_scripts/langgraph_fleet.py`.
2. Determinar por qué se aplicó a 4 archivos del mismo ciclo pero no a `payment-service.ts`, que tuvo un porcentaje de cambio (~65%) muy por encima del umbral de 30%. Hipótesis a verificar: ¿el guardia solo corre sobre archivos que superan cierto tamaño mínimo? ¿Se aplica por orden de escritura y algo interrumpe el chequeo a mitad de lote? ¿Hay una lista de archivos "ya revisados en un ciclo anterior" que se excluye de re-chequeo en ciclos posteriores (y `payment-service.ts` se escribió primero en un ciclo temprano que no tenía el guardia activo todavía, mientras que los otros 4 se reescribieron en un ciclo posterior que sí lo tenía)?
3. Una vez identificada la causa, corregir para que el guardia se aplique de forma uniforme a TODOS los archivos modificados en cada ciclo, sin excepciones silenciosas.

## Sobre el falso positivo en las 3 rutas de dashboard (nota, no bloqueante)

Sería valioso que el guardia pudiera distinguir "reescritura no solicitada" de "simplificación masiva intencional" cuando el `requerimiento`/plan explícitamente pide convertir un archivo largo en un adaptador delgado. Sugerencia: si el `requerimiento` contiene lenguaje explícito tipo "reduce a un adaptador delgado" / "reemplaza toda la lógica inline por una llamada al servicio", relajar o saltar el guardia para ese archivo específico. No es urgente — el rechazo conservador en ese caso es preferible a aprobar reescrituras destructivas reales, solo cuesta un ciclo extra de iteración.

## Criterios de aceptación

1. Un archivo con >30% de líneas cambiadas debe ser rechazado por el guardia de forma consistente, sin importar en qué archivo del lote se encuentre o en qué ciclo se haya escrito.
2. Test de regresión: simular un ciclo que escribe 2 archivos, ambos con >30% de cambio — confirmar que AMBOS se rechazan, no solo uno.
3. Revisar retroactivamente si algún otro despacho ya mergeado en `app-tennis` (Plan 1 o Plan 2 de este refactor) pudo haber tenido el mismo problema sin ser detectado — recomendación: re-auditar los diffs ya mergeados de este refactor buscando cambios no solicitados de tamaño similar, ya que no se puede confiar en que el guardia los hubiera atrapado.

## Verificación

**Investigación (punto 1 y 2):** la guarda de "reescritura excesiva" es la
que se agregó en el requerimiento 08 (`_apply_workspace_changes`,
`agile_scripts/langgraph_fleet.py`) — no es un guardia nuevo/no documentado,
solo que este reporte no tenía visibilidad de esa sesión anterior. Se
confirmó leyendo el código que **no existe** ninguna lógica de exclusión,
orden de escritura, ni lista de "archivos ya revisados en un ciclo anterior"
— `_apply_workspace_changes` es stateless y procesa cada archivo del lote de
forma uniforme en cada llamada. La causa real y única es el umbral: el
requerimiento 08 lo fijó en `similarity < 0.20` (>80% cambiado). Los 4
archivos rechazados en este ticket tenían 86-93% de cambio (por encima de
80%, correctamente rechazados); `payment-service.ts` tuvo ~65% (por debajo
de 80%, por eso pasó). No hay inconsistencia de aplicación — es un umbral
calibrado más laxo de lo que este caso real necesitaba.

**Fix (punto 3):** se bajó el umbral de `similarity < 0.20` a
`similarity < 0.70` (de >80% a >30% de cambio), alineado explícitamente con
el criterio de aceptación 1 de este mismo requerimiento y con el resto de
las guardas de la familia (truncamiento, req 06, también usa 30%).

Tests de regresión en `tests/test_anti_truncation_guard.py` (criterio 2):

```
test_rechaza_reescritura_de_65_por_ciento_que_antes_pasaba_el_umbral_viejo PASSED
test_ambos_archivos_del_mismo_ciclo_se_rechazan_si_ambos_superan_el_umbral PASSED
```

El primero reproduce los números reales del caso (177 líneas originales, 111
resultantes, ~65% de cambio) y confirma que ahora se rechaza. El segundo
cubre el criterio de aceptación 2 explícitamente (2 archivos en el mismo
ciclo, ambos por encima del umbral, ambos rechazados).

**Punto 3 (auditoría retroactiva de despachos ya mergeados en `app-tennis`):
no se hizo desde acá** — es una revisión manual de diffs ya mergeados en
otro proyecto, no un fix de código de la flota. Queda para que el usuario
(o quien mergeó esos PRs) la haga con el contexto real del refactor.

Nota sobre el punto no bloqueante (falso positivo en reescrituras
intencionales tipo "convertir a adaptador delgado"): no se implementó —
sigue siendo una mejora futura opcional, tal como el propio requerimiento la
marca como no urgente.
