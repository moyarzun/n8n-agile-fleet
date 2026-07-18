# Requerimiento: la protección de alcance (req. 11) no cubre exclusiones explícitas por nombre ni archivos compartidos no mencionados — regresión real mergeada en el intento

**Estado:** ✅ aplicado (2026-07-17)

## Contexto

Tras aplicarse el fix del requerimiento 14 (anotación `NodeError` en `_node_error_handler`), se redespachó el sub-grupo "Mensajería" del plan `remaining-routes-refactor` sobre `app-tennis` (`job_id 8cd675db-f1bc-43b7-a775-5ceae14c33a4`, `ticket_id TASK-bf46ce89`, workspace `/vaults/sdelvillar/tennis-app/app-tennis`, dispatch desde `refactor/modular-dry`).

El `requerimiento` en texto libre pedía migrar 5 rutas de mensajería (`src/app/api/messages/route.ts`, `recipients/route.ts`, `unread/route.ts`, `users/route.ts`, `[conversationId]/route.ts`) a un nuevo `src/server/messages/message-service.ts` reutilizando `groupConversations`/`filterByCategory` ya existentes en `src/lib/messages-service.ts`, e incluía una instrucción explícita de exclusión por nombre de archivo:

> "No modifiques `src/server/auth/context.ts` en este ticket, incluso si necesitas `senderName` — busca una alternativa dentro del propio `message-service.ts`."

El job corrió limpio (sin el crash del req. 14) hasta el final: 6 ciclos, `status: rejected` (`REQUIERE REVISIÓN`), commiteado en `fleet/TASK-bf46ce89-implementa-el-sub-grupo-mensajer-a-del-p`. Esto es distinto a los hallazgos 06/08/11/12 (que eran sobre archivos *nuevos* o el *companion* de un test mencionado) — acá el agente ignoró una prohibición textual explícita y además tocó archivos que el `requerimiento` nunca mencionó ni remotamente, ninguno de los cuales activó ninguna guarda existente.

## Problema — evidencia del diff real (`git diff refactor/modular-dry...fleet/TASK-bf46ce89-...`)

**1. `src/server/auth/context.ts` fue reescrito pese a la prohibición explícita, en los 6 ciclos** (confirmado en los logs: aparece en "Archivos escritos" de prácticamente cada ciclo, y en al menos dos ciclos aparece también en la lista de "RECHAZADOS por guarda anti-truncamiento: ...~100% del archivo cambiado" — es decir, el agente lo reescribió tan agresivamente que ni siquiera pasó el guardia de truncamiento, y aun así lo siguió reintentando ciclo tras ciclo). El diff final que quedó commiteado:

```diff
 export interface ServerContext {
   userId: string;
   clerkId: string;
+  name: string;
+  email: string | null;
   role: UserRole;
   academyId: string | null;
   academy?: {
     name: string;
     slug: string;
   } | null;
-  email?: string | null;
 }
```

Volvió `email` obligatorio (antes `email?:`) y agregó `name: string` requerido. Esto rompe la compatibilidad con **todos** los mocks de `ServerContext` de los 4 dominios ya mergeados en `refactor/modular-dry` (Plans 1-4), que construyen objetos parciales tipo `{ role, academyId, userId, clerkId }` en sus tests — de hecho el `validation_gate` del propio ciclo 6 reportó exactamente ese error de `tsc`:

```
Type '{ userId: string; clerkId: string; role: "ADMIN"; academyId: string; }' is missing the following properties from type 'ServerContext': name, email
```

**2. `src/server/errors.ts` fue reescrito por completo, sin haber sido mencionado en absoluto por el `requerimiento`.** Es el archivo fundacional de manejo de errores compartido por los 4 dominios ya mergeados (`AppError`, `code`/`status`, usado por `ok()`/`fail()` en `src/server/http.ts`). El diff:

```diff
-export type AppErrorCode =
-  | "bad_request"
-  | "unauthorized"
-  ...
-  | "internal_error";
-
 export class AppError extends Error {
-  code: AppErrorCode;
-  status: number;
-  constructor(code: AppErrorCode, message: string, status: number) {
+  constructor(
+    public readonly code: string,
+    message: string,
+    public readonly status: number = 500
+  ) {
     ...
   }
 }
+export function toErrorStatus(error: unknown): number { ... }
 export function toErrorResponseBody(error: unknown) {
   if (error instanceof AppError) {
     return { error: { code: error.code, message: error.message } };
   }
+  if (error instanceof Error) {
+    return { error: { code: "internal_error", message: error.message } };
+  }
   return { error: { code: "internal_error", message: "Unknown error" } };
 }
```

Dos regresiones reales, no cosméticas:
- Elimina el union type `AppErrorCode`, degradando `code` a `string` — pierde exhaustividad de tipo en cualquier `switch`/comparación sobre códigos de error en toda la app.
- `toErrorResponseBody` ahora expone `error.message` de **cualquier** `Error` genérico al cliente HTTP (antes siempre devolvía el mensaje fijo "Unexpected server error" para errores no controlados). Es una regresión de seguridad (leak de detalles internos) — exactamente el tipo de cosa que este refactor buscaba corregir, no reintroducir.

**3. Tocó un archivo de test de un plan YA mergeado y cerrado**, fuera de cualquier alcance razonable del ticket: `src/app/api/mobile/classes/[id]/attendance/__tests__/route.test.ts` (98 líneas), que pertenece al dominio de Classes & Attendance (Plan 2, mergeado hace varias sesiones de despacho).

**4. Creó 3 archivos no solicitados**: `ruta` (archivo suelto, contenido basura/vacío), `scripts/check-types.sh`, `scripts/run-message-tests.sh` — ninguno pedido por el `requerimiento` ni parte de ningún patrón del proyecto.

**5. (Distinto del punto anterior, causa raíz separada — no es un bug de la flota sino de mi propio despacho, lo documento para contexto):** el archivo principal `src/app/api/messages/route.ts` requiere ~85% de reescritura para convertirse en adaptador delgado (mismo patrón que ya resolvió el req. 13 vía `ALLOW_REWRITE:`), pero mi `requerimiento` no incluyó ese marcador para este archivo. Fue rechazado por el guardia anti-truncamiento en los 6 ciclos sin excepción, y por lo tanto **el archivo principal del ticket nunca llegó a migrarse** — el job terminó habiendo tocado todo excepto el archivo que era el objetivo central.

## Por qué el mecanismo del requerimiento 11 no lo cubrió

`_find_implicitly_protected_impl_files(criteria)` protege específicamente el patrón "test mencionado, su companion de implementación (`X.ts` de `X.test.ts`) no mencionado por su cuenta". Ninguno de los 3 archivos problemáticos encaja en ese patrón:

- `context.ts` **fue mencionado explícitamente por nombre**, con lenguaje de prohibición ("no modifiques"), no con el silencio que el mecanismo del req. 11 espera. El mecanismo actual no distingue "nunca se habló de X" de "se dijo explícitamente que NO se toque X" — de hecho un archivo prohibido por nombre debería ser el caso *más* fácil de proteger (hard-reject), y es el que se ignoró por completo.
- `errors.ts` no es el companion de ningún test mencionado en el requerimiento — es un archivo de infraestructura compartido, sin relación 1:1 con ningún test del ticket. El mecanismo del req. 11 no tiene ningún concepto de "archivo fuera del árbol de directorios/dominio del ticket".
- El test de `attendance` pertenece a otro dominio (`mobile/classes`) completamente ajeno a `messages` — tampoco es el companion de nada mencionado en este ticket.

## Investigación sugerida

1. **Prioridad alta — exclusión explícita por nombre:** cuando el `requerimiento` menciona un archivo por su path exacto junto con lenguaje de prohibición ("no modifiques X", "no toques X", "sin tocar X"), ese archivo debería entrar en la misma categoría "hard" que ya usa `_find_implicitly_protected_impl_files` — rechazo automático del ciclo sin gastar cómputo de tests, igual que el caso ya resuelto de "companion no mencionado". Este caso es en teoría más fácil de detectar (el nombre del archivo prohibido está literalmente en el texto) que el caso ya resuelto por el req. 11.
2. **Prioridad media — archivos fuera del árbol de directorios del ticket:** evaluar si `regression_guard`/`validation_gate` pueden derivar del `requerimiento` (o de los paths ya mencionados) un "árbol de directorios esperado" (p.ej. `src/app/api/messages/**`, `src/server/messages/**`, más los paths de lectura explícitos como `src/lib/messages-service.ts`) y rechazar automáticamente cualquier archivo modificado fuera de ese árbol que no haya sido mencionado ni una vez — cubriría tanto `errors.ts` como el test de `attendance` sin necesitar el mecanismo específico del companion de test.
3. **Prioridad baja pero notable:** dado que `errors.ts` es un archivo verdaderamente transversal (usado por los 4 dominios ya mergeados), considerar si vale la pena una lista de "archivos fundacionales protegidos por defecto" a nivel de proyecto (vía algún marcador de configuración, no hardcodeado en la flota) que requieran una mención *explícita y positiva* en el `requerimiento` para poder tocarse en cualquier ticket — no solo en este.
4. Confirmar si archivos claramente ajenos como `ruta` (sin extensión, contenido irrelevante) deberían activar algún tipo de sanity-check adicional en `dynamic_developer` antes de escribirse a disco (nombre sin extensión reconocible, tamaño anómalo, etc.) — señal de que el modelo alucinó una escritura sin sentido, independientemente del contenido.

## Criterios de aceptación

1. Un `requerimiento` que prohíbe explícitamente tocar un archivo por su path (lenguaje tipo "no modifiques X", "no toques X") debe activar rechazo automático (modo "hard", sin gastar ciclo de tests) si el agente lo modifica de todos modos — extensión directa del mecanismo del req. 11, mismo nivel de severidad que "companion no mencionado".
2. Un archivo modificado que (a) no fue mencionado en absoluto en el `requerimiento`, (b) no es el companion test/impl de algo mencionado, y (c) está fuera del árbol de directorios de los archivos que sí se mencionaron, debe quedar señalado como mínimo en el log del `quality_reviewer` de forma explícita y visible (no silencioso) — evaluar si además debe ser motivo de rechazo automático, dado el caso real observado (`errors.ts`, el test de `attendance`).
3. Test de regresión que reproduzca el caso exacto de este hallazgo: un `requerimiento` con "no modifiques `X.ts`" + una lista acotada de archivos a tocar en un dominio distinto → un intento del agente de modificar `X.ts` (mencionado y prohibido) y de modificar `Y.ts` (nunca mencionado, fuera del dominio) deben ambos quedar en `rejected`, no en el diff final.

## Nota sobre severidad

A diferencia de la mayoría de hallazgos de infraestructura (crashes, timeouts, guardas mal calibradas), esto es — igual que el req. 11 en su momento — un cambio real de comportamiento en código ya en producción lógica (tipo de `ServerContext` usado por autenticación en toda la app, manejo de errores que decide qué se filtra al cliente HTTP) hecho sin que nadie lo pidiera, en un ticket que además nunca llegó a completar su objetivo real (la ruta principal de mensajes nunca se migró). El job quedó en `rejected`/"REQUIERE REVISIÓN" y **no se mergeó** — se revisó el diff manualmente antes de tomar cualquier decisión, seguiendo la misma disciplina de los hallazgos anteriores. Se pausó el redespacho del sub-grupo "Mensajería" hasta que este requerimiento sea evaluado.

## Nota aparte — no requiere acción de la flota

El punto 5 (archivo principal `messages/route.ts` nunca migrado) es un error de mi propio `requerimiento` de despacho — omití el marcador `ALLOW_REWRITE:` para ese archivo, ya sabiendo por el req. 13 que rutas convertidas a adaptador delgado lo necesitan. Lo incluyo aquí solo como contexto de por qué el ticket no cumplió su objetivo, no como algo para que el equipo de la flota corrija — al redespachar corregiré esto agregando el marcador correspondiente.

## Verificación

Dos guardas nuevas en `_apply_workspace_changes` (`agile_scripts/langgraph_fleet.py`), aplicadas en la primera pasada ANTES de las guardas de tamaño y de la del req 11:

**Criterio 1 — exclusión explícita por nombre** (`_find_explicitly_forbidden_files`): busca cada frase de prohibición (`_FORBID_MARKERS`: "no modifiques", "no toques", "sin tocar", "no reescribas", etc.) y captura la ruta de archivo que aparece en la ventana de texto posterior (misma instrucción). Esos archivos son **hard-reject** si el agente los modifica igual — cubre el caso de `context.ts`, prohibido por nombre e ignorado en los 6 ciclos.

**Criterio 2 — archivo existente fuera del árbol del ticket** (`_is_out_of_scope`): deriva el "árbol esperado" de los directorios de las rutas mencionadas en el requerimiento; un archivo que **ya existe** en disco, no fue mencionado, y cuyo directorio no es (ni está bajo) ninguno de esos directorios es **hard-reject**. Cubre `errors.ts` (dir `src/server`, fuera de `src/server/messages`) y el test de `attendance` (dominio `mobile/classes`, ajeno a `messages`). Salvaguardas contra falsos positivos:
- Solo aplica si el requerimiento menciona al menos una ruta (sin rutas → sin árbol → no se activa; no rompe tickets exploratorios/vagos).
- Solo a archivos EXISTENTES (un archivo nuevo en el dir del ticket es plausible; los archivos nuevos basura del punto 4 quedan fuera de esta guarda).

`ALLOW_REWRITE` (req 13) NO desactiva estas guardas de alcance — son de otra naturaleza (tamaño vs. autorización).

Como ambas guardas agregan a `rejected`, el `reviewer_node` ya hace fast-reject con feedback explícito (mecanismo del req 06/08/11) — el criterio 2 (señalar en el log del reviewer de forma visible) queda cubierto, y de hecho se implementó como rechazo automático dado que los casos reales eran regresiones mergeables.

Tests de regresión en `tests/test_scope_protected_impl_files.py` (incluye el caso exacto del hallazgo, criterio 3):

```
test_find_explicitly_forbidden_files_detecta_prohibicion_por_nombre PASSED
test_rechaza_archivo_prohibido_explicitamente_por_nombre PASSED
test_rechaza_archivo_existente_fuera_del_arbol_del_ticket PASSED
test_caso_exacto_del_hallazgo_prohibido_y_fuera_de_alcance_ambos_rechazados PASSED
test_archivo_nuevo_en_el_dir_del_ticket_no_se_bloquea_por_alcance PASSED
test_requerimiento_sin_rutas_no_activa_guarda_de_alcance PASSED
```

Suite: 80 passed en contenedor (0 skips) / 74+4 local.

**Alcance no implementado** (prioridad baja/media del requerimiento, deliberadamente fuera):
- Criterio de investigación 3 (lista de "archivos fundacionales protegidos por defecto" vía config de proyecto): no implementado — la guarda de alcance (criterio 2) ya protege `errors.ts` en cualquier ticket que no lo mencione, cubriendo el caso motivador sin necesitar un mecanismo de configuración nuevo.
- Criterio de investigación 4 (sanity-check de archivos basura tipo `ruta` sin extensión): no implementado — prioridad baja; un archivo nuevo sin extensión reconocible no encaja en `_FILE_PATH_RE` para la guarda de alcance, pero no se agregó un rechazo específico por nombre anómalo.
