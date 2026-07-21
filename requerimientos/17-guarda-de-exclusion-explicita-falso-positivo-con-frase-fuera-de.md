# Requerimiento: la guarda de exclusión explícita (req. 11/15) da un falso positivo con la frase "no toques ningún archivo **fuera de** X" — bloquea el archivo central del ticket en el 100% de los ciclos

**Estado:** 🔴 abierto

## Contexto

Tras el cierre exitoso del sub-grupo "Mensajería" de `remaining-routes-refactor` (ver reqs 15/16, ambos verificados en producción real), se despachó el siguiente sub-grupo, "Waitlist": extender `src/lib/waitlist.ts` (archivo YA existente, con `promoteNextInWaitlist`/`getNextWaitlistPosition`) con 5 funciones nuevas/corregidas, y migrar 4 rutas (`src/app/api/waitlist/route.ts`, `waitlist/[classId]/route.ts`, `waitlist/join/route.ts`, `portal/waitlist/route.ts`) para consumirlas. `job_id cd574c8d-1346-48d5-9e8a-6a9988ff5f20`, `ticket_id TASK-96adcaf0`, rama `fleet/TASK-96adcaf0-migrar-el-sub-grupo-waitlist-del-plan-do`, workspace `/vaults/sdelvillar/tennis-app/app-tennis`.

El `requerimiento` en texto libre incluía, entre otras, esta línea de restricción de alcance (redactada para restringir el conjunto de archivos tocables — un **allow-list**, no un deny-list):

> "NO toques ningún archivo **fuera de** `src/lib/waitlist.ts`, `src/lib/waitlist.test.ts`, `src/app/api/waitlist/**`, y `src/app/api/portal/waitlist/**`."

La intención es obvia por el propio texto: "no toques nada fuera de estos 4 paths" = "solo puedes tocar estos 4 paths" (incluyendo, evidentemente, `src/lib/waitlist.ts`, que es justamente el archivo que el ticket pedía extender con 5 funciones nuevas en sus tareas A/B/C).

## Problema — evidencia del log (reproducido en los 6 ciclos, sin excepción)

En **todos y cada uno** de los 6 ciclos del job, el log de `dynamic_developer` muestra:

```
RECHAZADOS por guarda anti-truncamiento: src/lib/waitlist.ts: el requerimiento
prohíbe explícitamente modificar este archivo ('no modifiques/no toques …') —
rechazado por guarda de exclusión explícita; ...
```

(Confirmado buscando el patrón en el log completo: aparece a las 16:46:03, 16:50:15, 16:57:42, 17:01:26, 17:07:20, 17:11:47, 17:17:58, 17:20:36, 17:28:12, 17:29:39 — 10 apariciones en total a lo largo del job, una por cada intento de escritura del archivo en cada ciclo.)

Es decir: el mecanismo `_find_explicitly_forbidden_files` (agregado en el req. 15) tomó la frase "no toques ningún archivo **fuera de** `src/lib/waitlist.ts`, ..." y extrajo `src/lib/waitlist.ts` como el archivo prohibido — exactamente lo opuesto de lo que la frase dice. La palabra "fuera de" invierte el sentido de la restricción (de deny-list a allow-list), pero el mecanismo aparentemente solo busca el patrón `(no modifiques|no toques|...) ... <path>` sin verificar si hay un calificador de negación-de-negación como "fuera de" entre el verbo y el path — capturando el primer path que aparece después del verbo prohibitivo, sin importar el resto de la oración.

## Consecuencia real (no es solo un rechazo cosmético — el ticket completo quedó inservible)

Confirmado con `git diff refactor/modular-dry...fleet/TASK-96adcaf0-... --stat`: el diff final commiteado son **8 archivos, todos rutas y sus tests** (`waitlist/route.ts`, `waitlist/[classId]/route.ts`, `waitlist/join/route.ts`, `portal/waitlist/route.ts` + sus 4 `__tests__/route.test.ts`). **`src/lib/waitlist.ts` y `src/lib/waitlist.test.ts` NO aparecen en el diff en absoluto** — nunca se escribieron con éxito en ningún ciclo.

Como consecuencia, las 4 rutas migradas importan y llaman a `listWaitlistOverview`, `getClassWaitlist`, `joinWaitlist`, `getStudentWaitlistStatusForClass`, `listStudentWaitlistEntries` desde `@/lib/waitlist` — funciones que **no existen en ningún lado del árbol**, porque el único archivo que debía definirlas fue bloqueado en el 100% de los intentos. El ticket entero es inutilizable tal cual quedó: no compila, y aunque compilara, no hay ninguna implementación real detrás de las rutas migradas.

Este es un caso más severo que los hallazgos 06/08/11/12/13/15/16: no es una regresión parcial ni una inconsistencia de nombres — es el escenario donde una guarda mal calibrada bloquea el **objetivo central del ticket** de forma determinística en cada uno de los 6 ciclos, sin ninguna posibilidad de que el `dynamic_developer` lo resuelva (el archivo está fuera de su alcance en todos los intentos, es un rechazo automático "hard", no una sugerencia).

## Investigación sugerida

1. **Prioridad alta:** `_find_explicitly_forbidden_files` (o el regex que arma) debe reconocer calificadores de inversión de alcance entre el verbo prohibitivo y el path — como mínimo "fuera de", "excepto", "salvo", "que no sea". Cuando aparece alguno de estos inmediatamente antes del/los path(s), la frase es un **allow-list** (permite SOLO esos paths, prohíbe todo lo demás) y no debe agregar ninguno de los paths mencionados a la lista de archivos prohibidos — si acaso, debería invertir la lógica: todo archivo modificado que **no** esté bajo alguno de esos paths es lo que debería marcarse como fuera de alcance (ese es, de hecho, el mecanismo ya existente del req. 15 para "archivo fuera del árbol del ticket" — `_is_out_of_scope` — que ya cubre exactamente este caso correctamente cuando la frase no usa "no toques ... fuera de" sino una redacción directa de alcance).
2. **Investigación de causa raíz concreta:** ¿por qué el regex de `_find_explicitly_forbidden_files` no distingue este caso? Probablemente captura con algo como `(?:no modifiques|no toques)\s+.*?([\w/\-\.\[\]]+\.\w+)` (o similar) sin verificar qué palabras hay inmediatamente antes del path capturado. Revisar el patrón exacto en `agile_scripts/langgraph_fleet.py` y agregar una lista de "palabras de inversión" (`fuera de`, `excepto`, `salvo`, `que no sea`, `distinto de`) que, si aparecen entre el verbo y el path, invaliden esa coincidencia para el modo "hard" de esta guarda específica (dejando que `_is_out_of_scope` maneje el caso correctamente, como ya hace).
3. Nota para quien despache en el futuro mientras esto no esté arreglado: usar redacción de allow-list positiva ("Alcance permitido: SOLO puedes modificar/crear archivos dentro de: ...") en vez de la forma negativa con "fuera de", para evitar disparar este falso positivo — se usará este workaround en el redespacho de este mismo ticket.

## Segunda reproducción — el marcador "no reescribas" dispara el mismo falso positivo, no solo "fuera de"

El workaround de redacción positiva ("Alcance permitido: SOLO ...") sí resolvió el caso original — el redespacho del ticket completo de Waitlist (`TASK-a1b8d000`) logró escribir `src/lib/waitlist.ts` correctamente. Pero al despachar un ticket de fix mínimo sobre ese resultado (agregar 1 test a un `src/lib/waitlist.test.ts` YA EXISTENTE + arreglar 5 errores de tipo `Request`/`NextRequest` en 2 archivos de test), usando esta instrucción para el archivo de test:

> "src/lib/waitlist.test.ts (archivo YA EXISTENTE con 4 tests de promoteNextInWaitlist — **NO reescribas el archivo**, solo AGREGA un test nuevo al final del describe(...) existente...)"

y en las restricciones generales: "**No reescribas** ningún archivo completo, son ediciones puntuales." — **el mismo patrón se repitió exactamente igual**, dos veces consecutivas (`TASK-ce8abf86` y `TASK-4bdedf6f`, ambos con la misma redacción):

```
RECHAZADOS por guarda anti-truncamiento: src/app/api/portal/waitlist/__tests__/route.test.ts:
el requerimiento prohíbe explícitamente modificar este archivo ('no modifiques/no toques …')
— rechazado por guarda de exclusión explícita; src/lib/waitlist.test.ts: el requerimiento
prohíbe explícitamente modificar este archivo ('no modifiques/no toques …') — rechazado por ...
```

Esto confirma que `_FORBID_MARKERS` incluye "no reescribas" (documentado así en la Verificación del req. 15) como disparador de la misma guarda "hard" que "no modifiques"/"no toques" — pero "no reescribas X, solo agrega Y" es semánticamente una **instrucción de edición dirigida** (autoriza modificar el archivo de forma acotada, prohíbe específicamente la reescritura completa), no una prohibición total de tocarlo. El mecanismo trata ambas frases como equivalentes y bloquea el archivo por completo, impidiendo exactamente el tipo de edición pequeña y segura que la frase pedía.

Esto amplía el criterio de aceptación 1: no alcanza con manejar "fuera de" — cualquier marcador que en realidad autorice una edición acotada ("no reescribas X, solo agrega/cambia Y", "no reescribas X por completo", "edita X sin reescribirlo") necesita el mismo tratamiento que "fuera de": no debe agregar X a la lista de archivos prohibidos en modo "hard". Como estos matices de lenguaje natural son difíciles de cubrir exhaustivamente con reglas, considerar además si el modo "hard" debería relajarse a "soft" (solo log, no rechazo automático) para el marcador "no reescribas" específicamente, dado que por definición implica que SÍ se espera algún cambio en el archivo — a diferencia de "no modifiques"/"no toques", que si son genuinos no esperan ningún cambio en absoluto.

Workaround aplicado para seguir avanzando: evitar por completo las frases "no reescribas", "no modifiques", "no toques" cerca de cualquier nombre de archivo en los despachos siguientes, incluso cuando la intención sea autorizar una edición pequeña — describir el cambio en términos puramente afirmativos ("agrega X a este archivo, en esta ubicación exacta") sin ningún verbo prohibitivo.

## Criterios de aceptación

1. Un `requerimiento` que dice "no toques ningún archivo fuera de `X`, `Y`, `Z`" (o variantes con "excepto"/"salvo"/"que no sea") NO debe agregar `X`, `Y`, ni `Z` a la lista de archivos prohibidos por `_find_explicitly_forbidden_files` — estos paths son precisamente los que SÍ deben poder modificarse.
2. Test de regresión que reproduzca el caso exacto: un requerimiento con la frase "no toques ningún archivo fuera de `src/lib/waitlist.ts`" y un ticket que efectivamente escribe en `src/lib/waitlist.ts` → el archivo NO debe ser rechazado por la guarda de exclusión explícita.
3. Confirmar que la guarda `_is_out_of_scope` (req. 16) sigue funcionando normalmente para el caso complementario: un archivo que SÍ está fuera de los paths mencionados en una frase de este tipo debe seguir siendo rechazado por esa guarda (no por la de exclusión explícita) — no se debe romper la protección ya existente al arreglar este falso positivo.
4. Un requerimiento con la frase "no reescribas X, solo agrega/cambia Y" (o variantes que autoricen una edición acotada del mismo archivo) no debe rechazar en modo "hard" una escritura en X que efectivamente sea una edición pequeña y dirigida (no una reescritura completa) — reproducido 2 veces con `TASK-ce8abf86`/`TASK-4bdedf6f` sobre `src/lib/waitlist.test.ts` y `src/app/api/portal/waitlist/__tests__/route.test.ts`.

## Nota sobre severidad

Máxima entre los hallazgos de este refactor hasta ahora en términos de "ticket completamente inservible": a diferencia de Mensajería (donde al menos algo compilaba o se acercaba), acá el 100% del propósito del ticket (extender `waitlist.ts`) falló en el 100% de los ciclos por una causa mecánica y predecible, dejando un diff que ni siquiera podría revisarse como "parcialmente aprovechable" — las 4 rutas migradas dependen enteramente de funciones que no existen. No se mergeó nada. Redespacho pendiente, usando redacción de alcance positiva ("Alcance permitido: SOLO ...") como workaround mientras se corrige esta guarda.
