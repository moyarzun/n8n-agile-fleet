# Requerimiento: el agente `Node` no respeta rutas/nombres de archivo exactos cuando el requerimiento los especifica

**Estado:** ✅ investigado y aplicado (2026-07-16)

## Contexto

En el despacho `TASK-ff1ae0fb` sobre `app-tennis`, el `requerimiento` (texto libre) indicaba explícitamente: *"Lee el archivo completo primero — contiene el código exacto a escribir... No te desvíes de lo que el plan especifica: no agregues funcionalidad no pedida, no cambies nombres de funciones/archivos ya definidos en el plan"*. El plan referenciado (`docs/superpowers/plans/2026-07-15-payments-webhooks-refactor.md`) especificaba explícitamente crear `src/server/payments/webhook-service.ts` (un solo archivo) y extender `src/lib/clerk-webhook.ts` (archivo ya existente).

El agente `Node` en cambio creó:
- `src/server/payments/stripe-webhook-service.ts` (nombre distinto al pedido)
- `src/server/payments/clerk-webhook-service.ts` (archivo nuevo, no pedido — el plan pedía extender `src/lib/clerk-webhook.ts`)
- `src/server/payments/checkout-webhook-service.ts` (archivo adicional no mencionado en el plan en absoluto)
- `src/server/auth/clerk-webhook-service.ts` (una SEGUNDA versión del mismo concepto, en otra carpeta — duplicado propio dentro del mismo despacho)
- Modificó `src/server/payments/payment-service.ts` (el plan decía explícitamente usarlo "tal cual, no lo reinventes" como referencia de patrón, no como archivo a extender) agregándole una clase `PaymentService` nueva.
- **Borró** `src/server/payments/__tests__/payment-service.test.ts` (cobertura de funciones preexistentes, sin relación con este plan) y lo reemplazó por un archivo nuevo en otra ruta que solo cubre la clase nueva — pérdida silenciosa de cobertura de test para código que ni siquiera cambió.

No hubo daño funcional grave (las funciones originales de `payment-service.ts` quedaron con el mismo cuerpo), pero sí:
1. Duplicación propia dentro del mismo despacho (`clerk-webhook-service.ts` en dos carpetas distintas con propósito solapado).
2. Pérdida de cobertura de test de código no relacionado al plan.
3. Deriva de nombres que obliga a revisión manual cuidadosa antes de cada merge, en vez de poder confiar en que el código aterriza donde el requerimiento dijo.

## Problema (hipótesis, a confirmar por quien lo investigue)

No hay contexto suficiente en este documento para saber si esto es: (a) el LLM ignorando instrucciones explícitas de rutas por preferencia de su propio criterio de nombres, (b) el prompt interno de `dynamic_developer` no citando literalmente las rutas del plan (solo un resumen/paráfrasis se le pasa al modelo), o (c) el modelo perdiendo el nombre exacto entre ciclos (cada ciclo vuelve a generar desde el feedback del revisor, sin necesariamente releer el archivo del plan de nuevo).

## Investigación sugerida

1. Revisar qué contexto exacto recibe el agente `Node` en el prompt de `dynamic_developer` — ¿se le pasa el contenido completo del archivo del plan citado en el `requerimiento`, o solo el texto libre del `requerimiento` en sí? Si el plan vive en un archivo del repo (`docs/superpowers/plans/*.md`) y el `requerimiento` solo dice "léelo", confirmar que el pipeline efectivamente lee ese archivo del worktree y lo inyecta en el prompt — no asumir que el LLM "lo leyó" porque el requerimiento se lo pidió.
2. Si ya se inyecta el contenido completo del plan, considerar agregar una instrucción explícita y repetida en CADA ciclo (no solo el primero) del prompt de `dynamic_developer`/`quality_reviewer`: "los siguientes archivos DEBEN existir con estos nombres exactos: [lista extraída del plan]" — y que `regression_guard`/`validation_gate` verifiquen la presencia de esos nombres exactos como parte de la validación determinista, rechazando el ciclo si el agente creó un archivo con nombre distinto al esperado en vez de solo confiar en que tsc/vitest pasen.
3. Considerar que `quality_reviewer` (que sí revisa "criterios de aceptación") incluya explícitamente un chequeo de "¿los archivos creados coinciden con las rutas que el requerimiento/plan especificó?" como parte de sus criterios, no solo corrección funcional.

## Criterios de aceptación

1. Un despacho cuyo `requerimiento` cite un archivo de plan con rutas de archivo explícitas (`Create: \`ruta/exacta.ts\``, formato ya usado por los planes de `app-tennis` bajo `docs/superpowers/plans/`) debe producir archivos en esas rutas exactas — no nombres alternativos "equivalentes".
2. Si el agente considera que una ruta distinta es mejor, debe ser una decisión explícita y visible en el log/reporte final (ej. "usé X en vez de Y porque..."), no un cambio silencioso.
3. El agente no debe modificar ni borrar archivos fuera de las rutas que el plan declara como `Create`/`Modify` para las tareas en cuestión — si necesita tocar algo fuera de esa lista, debe reportarlo explícitamente en vez de hacerlo sin mencionarlo.

## Nota

Este comportamiento es más difícil de mitigar por completo que los bugs de infraestructura (01-07) — depende de cómo el LLM interpreta instrucciones, no solo de un fix determinístico de código. Priorizar la investigación del punto 1 (confirmar qué contexto realmente recibe el modelo) antes de invertir en mecanismos de enforcement más elaborados.

## Verificación (investigación punto 1 — causa raíz confirmada)

`codebase_reader_node` extrae rutas de archivo mencionadas en `subtasks` +
`acceptance_criteria` vía regex, y solo esas rutas se leen del worktree y se
inyectan en el prompt de `dynamic_developer` (bloques `===FILE_EXISTING===`).
La regex era:

```python
r'[\w/\-\.]+\.(?:ts|tsx|js|prisma|sql|rb|py|go|rs|json|yaml|yml)'
```

**No incluía `.md`.** Esto confirma sin ambigüedad la hipótesis (a)/(b) del
requerimiento: cuando el `requerimiento` libre dice "lee el archivo
`docs/superpowers/plans/X.md`", ese archivo NUNCA se lee ni se inyecta —
`dynamic_developer_node` es una llamada de completion (no un agente con
herramientas de filesystem), así que el modelo no tiene forma real de leer
ese archivo por su cuenta. Lo único que "ve" es el texto libre del
requerimiento pidiéndole que lo lea, sin el contenido — de ahí que alucine
una estructura/nombres propios en vez de reproducir el plan real.

Fix aplicado: se agregó `md` a la regex. Ahora un plan `.md` referenciado en
el requerimiento se lee (con el mismo cap de 80.000 caracteres que ya
aplicaba a los demás archivos) y se inyecta como `FILE_EXISTING` en cada
ciclo de `dynamic_developer`, exactamente igual que cualquier otro archivo
existente.

Test de regresión en `tests/test_codebase_reader_plan_files.py`:

```
test_codebase_reader_lee_archivo_de_plan_md_referenciado_en_el_requerimiento PASSED
```

Alcance: esto resuelve la causa raíz confirmada (investigación punto 1). Los
mecanismos de enforcement más elaborados sugeridos en investigación punto 2/3
(instrucción repetida cada ciclo listando nombres exactos, chequeo de
`quality_reviewer` sobre rutas) **no se implementaron** — el propio
requerimiento pide priorizar el punto 1 antes de invertir ahí, y con el plan
real ahora presente en el prompt (en vez de solo su nombre/resumen), el
modelo tiene el contenido real como referencia directa.
