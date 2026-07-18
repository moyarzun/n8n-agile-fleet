# Requerimiento: en tickets con muchos archivos interdependientes, el `dynamic_developer` converge a un estado internamente inconsistente — y en este caso perdió un endpoint completo

**Estado:** ✅ aplicado (2026-07-17) — criterios 1 y 2 (bloqueantes); criterio 3 evaluado, no implementado

## Contexto

Tras aplicarse el fix del requerimiento 15 (guardas de exclusión explícita por nombre y de alcance fuera del árbol del ticket), se redespachó el sub-grupo "Mensajería" de `remaining-routes-refactor` sobre `app-tennis`, esta vez corrigiendo también mi propio error anterior (agregando `ALLOW_REWRITE:` para las 5 rutas). `job_id 7da3e976-d6f7-46d9-bbe1-5a8389e2f98f`, `ticket_id TASK-7f7fe826`, rama `fleet/TASK-7f7fe826-migrar-el-sub-grupo-mensajer-a-del-plan`.

**El fix del req. 15 funcionó correctamente**: el diff final (12 archivos: 5 rutas + sus 5 tests + `message-service.ts` + su test) se mantuvo estrictamente dentro de `src/app/api/messages/**` y `src/server/messages/**`. No tocó `context.ts`, no tocó `errors.ts`, no creó archivos basura, no tocó nada de otro dominio. Confirmado con `git diff refactor/modular-dry...fleet/TASK-7f7fe826-... --stat`.

Sin embargo, el job terminó igual en `status: rejected` (`REQUIERE REVISIÓN`, ciclo 6/6, `validation_gate` con `tsc` en rojo). Revisé el branch manualmente en el worktree que la propia flota dejó
(`.fleet-worktrees/app-tennis-TASK-7f7fe826-migrar-el-sub-grupo-mensajer-a-del-plan`) corriendo yo mismo `npx tsc --noEmit --skipLibCheck`: **24 errores reales**, y uno de ellos no es solo un error de tipos — es una **pérdida de funcionalidad real**.

## Problema — evidencia concreta

### 1. (El más grave) Se perdió por completo el handler `GET` de `src/app/api/messages/[conversationId]/route.ts`

La ruta original en `refactor/modular-dry` (antes del ticket) tenía dos handlers:

```ts
// GET /api/messages/:partnerId — get messages in a conversation
export async function GET(req: NextRequest, { params }: ...) {
  // ... prisma.message.findMany con paginación (before/limit), orderBy desc, take: limit
  // ... luego prisma.message.updateMany para marcar como leídos
}
```

(más un `POST` para otra acción). El commit final de la flota dejó **solo**:

```ts
export async function POST(_req: Request, { params }: ...) {
  const ctx = await getServerContext();
  assertAcademy(ctx);
  const { conversationId } = await params;
  const result = await markConversationAsRead({ academyId: ctx.academyId, clerkId: ctx.clerkId }, conversationId);
  return ok(result);
}
```

El `GET` desapareció sin dejar rastro — no hay ninguna función equivalente en `message-service.ts` (confirmado: `grep -n "conversationId\|partnerId\|listConversationMessages\|getConversationMessages" src/server/messages/message-service.ts` no encuentra ninguna función que liste mensajes de una conversación específica con paginación). Es decir, el endpoint que un cliente (web o mobile) usa para **leer el historial de mensajes de una conversación** dejó de existir. El `requerimiento` nunca pidió eliminar ningún endpoint — pedía migrar los handlers existentes al nuevo patrón, preservando su comportamiento.

El test de este archivo (`__tests__/route.test.ts`, escrito en un ciclo anterior antes de que el `GET` se perdiera) sigue esperando `GET`, por eso `tsc` marca 3 veces `Property 'GET' does not exist on type ...` — es la señal que expuso el problema, pero el problema real es la pérdida del endpoint, no el error de tipos en sí.

### 2. Nombres de funciones inconsistentes entre `message-service.ts` y sus consumidores

`message-service.ts` exporta:
```
markConversationRead(...)
getUnreadCount(...)
```

Pero `route.ts`/`unread/route.ts` (escritos en ciclos distintos del mismo servicio) importan:
```
markConversationAsRead   // no existe — TS2724, "¿quisiste decir markConversationRead?"
getUnreadMessageCount    // no existe — TS2724, "¿quisiste decir getUnreadCount?"
```

Es decir, dentro del mismo ticket y aparentemente del mismo cliclo final, el nombre que el modelo usó al **definir** la función en `message-service.ts` no coincide con el nombre que usó al **consumirla** desde las rutas — un problema de contrato de nombres no estable entre archivos que se supone pertenecen al mismo cambio coordinado.

### 3. Tests con firmas que no coinciden con la implementación final

- `src/app/api/messages/__tests__/route.test.ts` y `.../recipients/__tests__/route.test.ts` llaman a `GET`/`POST` pasando un `Request` (`Request` global de Node/fetch) donde la implementación tipa el parámetro como `NextRequest` de `next/server` — `tsc` falla con `TS2345` (6 ocurrencias). Los tests fueron escritos para una firma distinta a la que terminó la implementación.
- `src/app/api/messages/unread/__tests__/route.test.ts` llama a `GET(algúnArgumento)` pero la implementación final no toma parámetros (`TS2554: Expected 0 arguments, but got 1`).

### 4. Mock de Prisma incompleto en `message-service.test.ts`

10 errores `TS2339: Property 'findMany' does not exist` sobre el mock de `prisma.message`/`prisma.studentAcademy` (tipado en el test como `{ findFirst: Mock; findUnique: Mock }`, sin `findMany`) — el test fue escrito asumiendo que la implementación usaría `findFirst`/`findUnique`, pero la implementación final sí llama a `findMany` en algunos casos, y el mock no se actualizó para reflejarlo.

### 5. `src/app/api/messages/route.ts` referencia `ctx.name`, que no existe

```ts
error TS2339: Property 'name' does not exist on type 'ServerContext & { academyId: string; }'.
```

Esto es la otra cara del punto 1 del req. 15: **la guarda de exclusión explícita bloqueó correctamente la modificación de `context.ts`** (no se agregó `name` al tipo, como sí había pasado en el intento anterior), pero el modelo **no implementó la alternativa que el `requerimiento` pedía explícitamente** ("si necesitas el nombre del remitente, resuélvelo dentro de `message-service.ts` haciendo tu propio lookup de `User`") — simplemente dejó código en `route.ts` que asume el campo existe en `ServerContext`, sin escribir el lookup alternativo en ningún lado. Es decir, la guarda evitó la regresión de tipo compartido, pero el ticket quedó con una referencia rota en su lugar — el modelo no reaccionó a la restricción con la solución pedida, solo dejó de violar la prohibición literal.

## Por qué esto es distinto de los hallazgos 06/08/11/12/13/15

Todos esos son sobre **archivos fuera de lo pedido** (nuevos no autorizados, companions no mencionados, árbol fuera de dominio) o sobre **guardas de tamaño mal calibradas**. Acá el alcance de archivos fue perfecto — el problema es que, dentro del conjunto correcto de archivos, el contenido que distintos ciclos escribieron para "el mismo cambio" no es mutuamente consistente: nombres de función que no coinciden entre definición y uso, tests que no coinciden con la firma final de la implementación, y en el caso más grave, una porción entera de funcionalidad (el `GET`) que simplemente se perdió en el camino sin que ningún ciclo la haya vuelto a escribir.

## Investigación sugerida

1. **Prioridad alta — pérdida de funcionalidad silenciosa:** investigar por qué el `GET` de `[conversationId]/route.ts` desapareció. Hipótesis: el `codebase_reader` inyectó el contenido original de la ruta como contexto de lectura, pero en algún ciclo el `dynamic_developer` reescribió el archivo completo (bajo el permiso de `ALLOW_REWRITE`) y el modelo, al generar el nuevo contenido, simplemente omitió portar el handler `GET` — ninguna guarda detecta "un export público que existía en el archivo original ya no existe en la versión nueva". Esto parece ser exactamente el tipo de pérdida que `regression_guard` fue diseñado para atrapar (ver mecanismo ya usado en el hallazgo 11, donde restauró 3 archivos con exports `GET`/`dynamic` borrados accidentalmente) — pero acá no se disparó. Confirmar si `regression_guard` compara exports públicos del archivo antes/después cuando el archivo fue reescrito bajo `ALLOW_REWRITE`, o si el marcador desactiva también esa comparación (no debería: `ALLOW_REWRITE` es sobre el guardia de tamaño/truncamiento, no debería eximir la detección de exports públicos perdidos).
2. **Prioridad media — contrato de nombres estable entre ciclos:** cuando `message-service.ts` y sus consumidores (`route.ts` de distintas carpetas) se escriben en ciclos distintos dentro del mismo ticket, considerar si conviene que `dynamic_developer` reciba como contexto explícito los nombres de función ya exportados por archivos del propio ticket escritos en ciclos anteriores (no solo el contenido completo, sino una lista corta de "firmas públicas ya establecidas en este ticket") para reducir la deriva de nombres entre ciclos.
3. **Prioridad media — tests vs. firma final:** evaluar si `validation_gate` puede, antes de aceptar un ciclo, correr un chequeo de tipos incremental que capture específicamente mismatches `test.ts` ↔ `route.ts`/`service.ts` del mismo ticket (ya lo hace de hecho — el `tsc` del ciclo 6 reportó todo esto — el punto es que el ciclo 6 es el último y el ticket se agota ahí; ¿vale la pena que el feedback de ciclos intermedios incluya el resumen de `tsc` para que el `dynamic_developer` lo corrija en el propio ciclo siguiente, en vez de solo el resumen del guardia de truncamiento?).

## Criterios de aceptación

1. Cuando `dynamic_developer` reescribe completamente un archivo bajo `ALLOW_REWRITE`, `regression_guard` debe seguir comparando el conjunto de exports públicos (funciones exportadas a nivel de módulo, en particular handlers HTTP como `GET`/`POST`/`PUT`/`DELETE`/`PATCH` en archivos `route.ts`) entre la versión original y la nueva, y marcar como regresión (igual que ya hace para exports "accidentalmente borrados", mecanismo usado en el hallazgo 11) cualquier export público presente en el original y ausente en la nueva versión — `ALLOW_REWRITE` autoriza reescribir el contenido, no perder capacidades públicas del archivo.
2. Test de regresión que reproduzca el caso exacto: un archivo `route.ts` con `GET` y `POST` es reescrito bajo `ALLOW_REWRITE` dejando solo `POST` → debe quedar señalado como regresión antes de llegar a `validation_gate`.
3. (Alcance abierto a discusión, no bloqueante) Evaluar si vale la pena una mejora para el problema de nombres inconsistentes (punto 2 de este hallazgo) y si el feedback de `tsc` de ciclos intermedios debería propagarse de forma más explícita al `dynamic_developer` del ciclo siguiente — no es necesariamente un bug, puede ser un ajuste de prompt/feedback de bajo riesgo.

## Nota sobre severidad

Distinto de todos los hallazgos anteriores: acá no hay una regresión de código *ya en producción* (los 4 dominios previos siguen intactos, confirmado por el alcance limpio), pero el ticket en sí **retrocede una funcionalidad existente** (lectura de historial de una conversación) si se llegara a mergear tal cual — y a diferencia del hallazgo 12 (payment-service.ts), acá ni siquiera hizo falta revisar minuciosamente línea por línea: `tsc --noEmit` lo expuso de inmediato porque el test correspondiente todavía esperaba el `GET`. El job quedó en `rejected` y **no se mergeó**. Redespacho de "Mensajería" pausado hasta que esto se evalúe.

## Verificación

**Causa raíz confirmada** (distinta a la hipótesis de "ALLOW_REWRITE desactiva regression_guard"): `ALLOW_REWRITE` NO toca `regression_guard` (son nodos distintos; el marcador solo salta las guardas de tamaño en `_apply_workspace_changes`). El problema real: `regression_guard` solo compara los archivos que `codebase_reader` capturó en `existing_files`, y `codebase_reader` extrae rutas con la regex `[\w/\-\.]+\.(ts|...)` que **no matchea los corchetes de las rutas dinámicas de Next.js** (`[conversationId]`). Así el original de `[conversationId]/route.ts` nunca se leyó → `regression_guard` no tenía un "antes" contra qué comparar → la pérdida del `GET` pasó inadvertida. (De hecho `_check_ts_exports_regression` ya detectaba exports eliminados desde el hallazgo 11 — el mecanismo funcionaba, pero nunca recibió el original de este archivo.)

**Fix (criterios 1 y 2):**
- Nueva función `_git_original_content(workspace, rel_path)`: lee el archivo desde `git show HEAD:<path>` en el worktree. Como la Flota solo commitea en `git_finalize`, HEAD == la base del ticket durante todos los ciclos, así que da el "antes" real de CUALQUIER archivo modificado.
- `regression_guard_node` ahora **augmenta** `existing_files` con el original desde git para todo archivo aplicado (`.ts/.tsx/.js`/`schema.prisma`) que `codebase_reader` no había capturado — cubre rutas dinámicas con corchetes y archivos no mencionados. `ALLOW_REWRITE` no lo exime (autoriza reescribir el contenido, no perder capacidades públicas). El `existing_files` augmentado se propaga al estado para que el próximo ciclo del `dynamic_developer` vea el original recuperado.
- `_check_ts_exports_regression` marca explícitamente cuando el export perdido es un handler HTTP (`GET/POST/PUT/DELETE/PATCH/HEAD/OPTIONS`) — "endpoint(s) perdido(s)".
- Prevención complementaria: la regex de extracción de rutas (en `codebase_reader` y en `_FILE_PATH_RE` de las guardas del req 15) ahora incluye `[]`, así las rutas dinámicas se leen como contexto desde el ciclo 1 y el modelo las ve.

Tests de regresión en `tests/test_regression_guard_lost_exports.py`:

```
test_detecta_handler_GET_perdido_en_route_ts PASSED
test_no_marca_regresion_si_se_conservan_los_exports PASSED
test_regression_guard_detecta_GET_perdido_via_git_en_ruta_dinamica PASSED   (caso exacto, git real, criterio 2)
test_regression_guard_no_marca_archivo_nuevo_del_ticket PASSED               (caso negativo)
test_codebase_reader_regex_captura_ruta_dinamica_con_corchetes PASSED
```

El test del criterio 2 reproduce el hallazgo con un repo git real: `[conversationId]/route.ts` con `GET`+`POST` commiteado, reescrito dejando solo `POST`, `existing_files` vacío (como si `codebase_reader` no lo hubiera capturado) → `regression_guard` recupera el original desde git, marca la pérdida del `GET` y restaura el archivo antes de llegar a `validation_gate`.

Suite: 85 passed en contenedor (0 skips) / 79+4 local.

**Criterio 3 (no bloqueante) — evaluado, no implementado:** los puntos 2 (contrato de nombres estable entre ciclos: `markConversationRead` vs `markConversationAsRead`) y 3 (propagar el `tsc` de ciclos intermedios al `dynamic_developer`) del hallazgo son ajustes de prompt/feedback, no bugs. No se implementaron en esta iteración: son de bajo riesgo pero requieren tuning empírico del prompt para no inflar el contexto, y el criterio los marca explícitamente como "alcance abierto a discusión, no bloqueante". La deriva de nombres además queda expuesta por `validation_gate` (tsc) igual que la pérdida de endpoint — ahora, con la pérdida del endpoint atajada antes por `regression_guard`, el foco del feedback de tsc queda en los mismatches de nombres reales.
