# Requerimiento: el agente `Node` reescribe archivos de implementación aunque el requerimiento pida explícitamente tocar solo tests

**Estado:** ✅ aplicado (2026-07-16)

## Contexto

Tras aplicar el fix del requerimiento 08 (`codebase_reader` ahora lee los `.md` de plan referenciados, resolviendo la causa raíz de "inventa nombres de archivo"), se despachó un fix puntual y acotado sobre `app-tennis` (`TASK-08cd65ac`) pidiendo corregir 5 problemas concretos, la mayoría en archivos `*.test.ts`. El requerimiento incluía instrucciones explícitas de alcance:

> *"Ajustar el TEST (no la implementación, salvo que confirmes que la implementación tiene el bug real)"* (punto 2, sobre `src/server/auth/clerk-webhook-service.test.ts`)

> Punto 5 solo mencionaba `src/app/api/stripe/webhooks/route.test.ts` — ni una palabra sobre tocar `route.ts` (la implementación).

## Problema

Pese a esas instrucciones, el diff resultante modificó también las implementaciones:

**`src/server/auth/clerk-webhook-service.ts`** (64 líneas cambiadas, archivo que el requerimiento nunca autorizó tocar): se reescribió la rama de "sin metadata de invitación" del caso `user.created`. La versión anterior distinguía tres casos — metadata completa (invitado), metadata totalmente ausente (crear owner nuevo), y metadata *parcial* (rol o academyId pero no ambos → `return { type: "ignored", reason: "Incomplete invitation metadata" }`). La nueva versión **eliminó por completo la validación de metadata parcial** — cualquier caso que no sea "ambos presentes" ahora crea una academia nueva sin condición, incluyendo el caso que antes se ignoraba explícitamente. Es un cambio real de comportamiento en el flujo de creación de usuarios/academias (área sensible), no una refactorización cosmética.

**`src/app/api/stripe/webhooks/route.ts`** (59 líneas cambiadas, archivo que el requerimiento ni mencionó — el punto 5 hablaba solo de `route.test.ts`): el `catch` genérico que devolvía `NextResponse.json({ error: { code: "internal_error", ... } }, { status: 500 })` para cualquier error no-`AppError` fue reemplazado por `throw error;` (re-lanzar sin capturar). Esto rompió 2 tests que verificaban ese comportamiento (`returns 500 when StripeWebhookService throws internal_error`, `returns 404 when service throws not_found error`) y, más grave, es un cambio real de robustez: un webhook de Stripe necesita devolver siempre una respuesta HTTP; un error no capturado que se propague al manejador genérico de Next.js puede no devolver el 500 limpio que Stripe espera para reintentar correctamente.

## Diferencia con el requerimiento 08

El 08 investigó y resolvió por qué el agente **inventaba archivos con nombres incorrectos** (no leía el plan real). Este es un problema distinto: el agente **sí toca los archivos correctos**, pero además modifica lógica que nadie pidió tocar, incluso cuando el `requerimiento` es explícito en decir "solo el test, no la implementación" o ni siquiera menciona el archivo de implementación en absoluto.

## Investigación sugerida

1. Confirmar si el prompt de `dynamic_developer` transmite instrucciones de alcance negativas ("NO toques X") con la misma fuerza que las positivas ("SÍ toca Y") — es posible que el modelo priorice "arreglar todo lo que ve mal" sobre restricciones explícitas de alcance, especialmente si al leer el archivo de implementación completo (inyectado como contexto para poder entender el test) el modelo decide "de paso" mejorarlo.
2. Considerar que `codebase_reader` NO inyecte el contenido completo de archivos de implementación que el `requerimiento` explícitamente excluye de edición — si el modelo no ve el archivo completo, no puede reescribirlo (aunque esto podría limitar su capacidad de entender si el test realmente refleja un bug real de la implementación, un trade-off a evaluar).
3. Alternativa más simple: que `regression_guard`/`validation_gate` chequeen que los ÚNICOS archivos modificados sean los mencionados explícitamente en el `requerimiento` (ya sugerido parcialmente en el criterio 3 del requerimiento 08, pero ahí se enfocaba en archivos NUEVOS creados — acá el problema es archivos EXISTENTES modificados de más). Si el diff toca un archivo no autorizado, rechazar el ciclo automáticamente con un mensaje claro ("modificaste X sin autorización") antes de correr tests, ahorrando ciclos completos de cómputo.

## Criterios de aceptación

1. Un requerimiento que dice explícitamente "ajusta el test, no la implementación" para un archivo dado no debe resultar en cambios a ese archivo de implementación, salvo que el agente reporte explícitamente por qué consideró necesario el cambio (visible en el log, no silencioso).
2. Un requerimiento que menciona solo `X.test.ts` sin mencionar `X.ts` no debe modificar `X.ts` en absoluto.
3. Si `validation_gate` puede derivar la lista de archivos "autorizados" a partir del `requerimiento` (mismo mecanismo ya usado para nombres de archivo en el req. 08), debe poder rechazar automáticamente un ciclo que modificó archivos fuera de esa lista, sin gastar un ciclo completo de tests para descubrirlo.

## Nota sobre severidad

A diferencia de la mayoría de los hallazgos anteriores (bugs de infraestructura, o pérdida de cobertura de test), este es un cambio de **lógica de negocio real en un flujo de autenticación/provisioning**, hecho sin que nadie lo pidiera ni lo revisara — el tipo de cambio que, si se mergea sin una revisión manual cuidadosa del diff, puede introducir un bug de producción silencioso. Reforzar la importancia de nunca mergear el resultado de un despacho sin revisar el diff completo, incluso cuando el `status` final sea "approved" (acá ni siquiera lo fue, pero el punto se sostiene igual).

## Verificación

Implementada la alternativa del investigación punto 3, acotada al patrón
concreto del bug (no un filtro genérico "solo lo que está nombrado"): nueva
función `_find_implicitly_protected_impl_files(criteria)` en
`agile_scripts/langgraph_fleet.py`. Deriva del texto libre del requerimiento
qué archivos de implementación NO están autorizados: el companion de
implementación (`X.ts`) de un test (`X.test.ts`/`X.spec.ts`) mencionado,
cuando ese companion nunca se menciona por su cuenta en otro lugar del texto.

Dos niveles de severidad (cubre el matiz entre el punto 2 y el punto 5 del
bug real, que tenían distinto lenguaje de alcance):
- **"hard"** (rechazo automático, sin gastar un ciclo de tests): no hay
  ningún lenguaje de excepción condicional en el requerimiento — este es
  exactamente el caso del punto 5 (`route.ts` nunca mencionado, cero
  lenguaje de permiso).
- **"soft"** (no bloquea, solo se registra en el log para el reviewer): el
  requerimiento contiene una frase de excepción condicional ("salvo que",
  "excepto si", "a menos que", "salvo si", "a no ser que") — el caso del
  punto 2 ("no la implementación, salvo que confirmes que la implementación
  tiene el bug real"), que sí contempla la posibilidad.

`_apply_workspace_changes` ahora recibe `criteria` (el `acceptance_criteria`
del ticket) y rechaza (agrega a `rejected`, no escribe el archivo) cualquier
archivo protegido en modo "hard" — reutilizando el mismo mecanismo de
`rejected_files` → fast-reject de `reviewer_node` ya establecido en los
requerimientos 06/08 (ningún ciclo con un archivo así puede aprobarse sin
gastar cómputo del LLM revisor).

Tests de regresión en `tests/test_scope_protected_impl_files.py` (cubren los
3 criterios de aceptación y los dos casos reales del bug):

```
test_protege_implementacion_no_mencionada_del_todo PASSED
test_no_protege_si_la_implementacion_tambien_se_menciona PASSED
test_protege_en_modo_soft_si_hay_lenguaje_de_excepcion_condicional PASSED
test_rechaza_implementacion_no_autorizada_cuando_solo_se_pidio_el_test PASSED
test_permite_implementacion_con_excepcion_condicional_pero_no_la_rechaza PASSED
test_sin_criteria_no_activa_ninguna_proteccion PASSED
```

Alcance: el mecanismo cubre específicamente el patrón "test mencionado,
implementación companion no mencionada" — no implementa un chequeo genérico
de "todo archivo modificado debe estar nombrado explícitamente en el
requerimiento" (sería mucho más propenso a falsos positivos para cambios
legítimos con archivos auxiliares no nombrados uno por uno).
