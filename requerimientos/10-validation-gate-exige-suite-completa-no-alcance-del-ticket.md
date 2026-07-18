# Requerimiento: `validation_gate`/`quality_reviewer` deben aprobar según el alcance del ticket, no exigir que TODA la suite del repo esté verde

**Estado:** ✅ aplicado (2026-07-16) — despachos desbloqueados

## Contexto

Al cerrar el plan `payments-webhooks-refactor` sobre `app-tennis` (rama de trabajo con un refactor grande en curso, múltiples archivos con fallos conocidos y ya rastreados por separado), se despacharon varios fixes puntuales muy chicos (una línea, un import faltante) para no volver a chocar con el límite de contexto (ver requerimiento 09). Cada uno de estos fixes triviales tardó **5-6 ciclos completos** (~40-60 min de cómputo LLM) antes de terminar en `rejected`, pese a que el cambio pedido era correcto.

## Evidencia concreta (el caso más claro)

Despacho: *"En src/server/payments/checkout-webhook-service.test.ts línea 54, falta importar afterEach de vitest — agregarlo al import existente. No tocar ningún otro archivo."*

Resultado final tras 6 ciclos, `status: rejected`:

```
TYPESCRIPT (tsc --noEmit): ✓ sin errores en archivos generados (ignorados: 41 línea(s) pre-existentes o módulos mobile)

VITEST (npx vitest run): ✗ exit 1
 FAIL  src/server/auth/clerk-webhook-service.test.ts > ClerkWebhookService.handleClerkEvent (auth) > user.deleted > returns ignored when user not found
AssertionError: expected { type: 'ignored', …(1) } to deeply equal { type: 'ignored', …(1) }
- "reason": "User not found",
+ "reason": "User not found in database",
```

El fix pedido (agregar `afterEach`) **se hizo correctamente** — TypeScript quedó limpio. El `rejected` final fue por **2 tests fallando en `src/server/auth/clerk-webhook-service.test.ts`**, un archivo que este ticket nunca mencionó ni tocó, con fallos preexistentes de otro trabajo (un mismatch de aserciones de un ticket anterior, ya identificado y pendiente de otro fix separado).

## Problema

`validation_gate` corre `npx vitest run` sin ningún filtro de alcance — evalúa la suite completa del repo, no solo los archivos que el `requerimiento` pidió tocar. En un repo con un refactor grande en curso (varios archivos con issues conocidos, cada uno rastreado y planificado para resolverse en su propio ticket), **cualquier fix, por más correcto y acotado que sea, queda bloqueado indefinidamente** mientras exista CUALQUIER otro test rojo en cualquier parte del repo — sin importar que ese test no tenga relación alguna con lo que el ticket pidió.

Esto explica por completo el patrón observado de "fixes triviales tardan 5-6 ciclos": el agente `Node` recibe feedback de "la validación falló" en cada ciclo y trata de adivinar qué más arreglar (a veces tocando código fuera de su alcance real, ver requerimiento 08), sin nunca poder tener éxito porque el criterio de aprobación no es "tu cambio es correcto" sino "el repo entero está 100% verde" — un criterio que ningún ticket acotado puede cumplir por sí solo en medio de un refactor con múltiples piezas en paralelo.

## Fix propuesto

`validation_gate` (o `quality_reviewer` al evaluarlo) debe distinguir entre:
1. **Regresiones introducidas por este ciclo** — tests que pasaban ANTES de este despacho y ahora fallan. Esto sí debe bloquear (es exactamente lo que `regression_guard` ya intenta hacer en otro paso, pero aparentemente solo compara archivos, no resultados de test).
2. **Fallos preexistentes no relacionados** — tests que ya fallaban antes de este despacho, en archivos que el ticket no tocó. Esto NO debería bloquear la aprobación del ticket actual.

Implementación sugerida: capturar el resultado de `npx vitest run` ANTES de que `dynamic_developer` haga cualquier cambio (baseline), y comparar contra el resultado DESPUÉS del cambio — aprobar si el conjunto de tests fallando después es un subconjunto del que ya fallaba antes (o vacío), rechazar solo si aparecen fallos NUEVOS que no estaban en el baseline.

## Criterios de aceptación

1. Un ticket que corrige exactamente lo que pide, sin tocar nada más, debe aprobarse aunque existan fallos preexistentes en otros archivos no relacionados — siempre que esos fallos preexistentes ya estuvieran presentes en el baseline (antes de este ticket).
2. Un ticket que introduce una regresión nueva (un test que pasaba y ahora falla) sí debe rechazarse, incluso si es en un archivo que "no debería haber tocado" — eso ya lo cubre `regression_guard`, pero conviene verificar que la comparación de resultados de test (no solo de archivos modificados) también entre en ese chequeo.
3. Test de regresión: simular un repo con 2 archivos de test, uno ya roto desde antes (baseline) y otro que el ticket debe arreglar — confirmar que el ticket se aprueba aunque el primero siga roto.

## Por qué esto es bloqueante para seguir usando la flota en `app-tennis`

Este refactor tiene 8 planes independientes, varios de los cuales tocan partes del código con fallos conocidos y ya planificados para resolverse en su propio plan/ticket (ver `docs/superpowers/plans/2026-07-15-*.md`). Sin este fix, **ningún ticket futuro podrá aprobarse limpiamente** hasta que absolutamente todo el refactor esté terminado de una sola vez — lo cual contradice el enfoque de "un dominio por vez, revisado y mergeado incrementalmente" que se usó para planificar todo este trabajo. Pausamos nuevos despachos sobre `app-tennis` hasta que esto se resuelva.

## Verificación

Implementado tal como se propuso: baseline capturado ANTES de que
`dynamic_developer` edite nada.

- `git_setup_node` corre `_run_vitest_baseline()` justo después de `npm
  install` (workspace todavía pristino, recién creado el worktree) y guarda
  el resultado en el nuevo campo de estado `validation_baseline_failing_tests`
  (lista de identificadores de tests que fallan, o `None` si vitest no aplica
  a este proyecto — se distingue explícitamente de "lista vacía" para no
  asumir de más cuando no hay baseline real).
- `validation_gate_node` pasa ese baseline a `_validate_workspace()`.
- En el paso de Vitest de `_validate_workspace`, si hay baseline disponible:
  se extraen los tests que fallan en la corrida POST-cambio
  (`_extract_failing_tests()`, parsea líneas `FAIL ...` del reporter
  verbose) y se comparan contra el baseline. Solo se rechaza si hay fallos
  **nuevos** (`current_failing - baseline_failing_tests`); los que ya
  fallaban antes se listan como informativos pero no bloquean.
- Si no hay baseline disponible (`None`), cae al comportamiento estricto
  anterior (cualquier fallo bloquea) — no hay forma segura de asumir qué era
  preexistente sin esa referencia.

Tests de regresión en `tests/test_validation_gate_baseline.py` (cubren los 3
criterios de aceptación):

```
test_extract_failing_tests_parsea_lineas_fail PASSED
test_ticket_acotado_se_aprueba_aunque_fallo_preexistente_siga_roto PASSED
test_regresion_nueva_introducida_por_el_ticket_se_rechaza PASSED
test_sin_baseline_disponible_cae_a_comportamiento_estricto_anterior PASSED
```

Alcance: el fix cubre específicamente el paso de Vitest (Nivel 1c de
`_validate_workspace`), que es el caso concreto reproducido y documentado
arriba. El "Nivel 2" (`bin/fleet-validate`/`FLEET_VALIDATE_CMD`, un script
opaco del proyecto) sigue siendo todo-o-nada como antes — no hay forma
genérica de parsear "qué test específico falló" de un script arbitrario sin
asumir su formato de salida. Si este patrón se repite con proyectos que
validan solo vía `bin/fleet-validate` (no Vitest directo), evaluar extender
el mismo mecanismo de baseline ahí.
