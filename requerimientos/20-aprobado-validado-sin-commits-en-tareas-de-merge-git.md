# Requerimiento: la Flota reporta "Aprobado+validado" en un ticket que no produjo ningún commit ni cambio

**Estado:** ✅ implementado (2026-07-23) — `validation_gate_node` rechaza explícitamente ciclos sin cambios efectivos (`_no_effective_changes`, `agile_scripts/langgraph_fleet.py`). Tests: `tests/test_validation_gate_no_effective_changes.py`.

## Contexto

Proyecto: `app-tennis` (workspace `/vaults/sdelvillar/tennis-app/app-tennis`).

Despaché dos tickets seguidos con `resolver_requerimiento` pidiendo mergear
`origin/main` dentro de `staging` y resolver un conflicto de merge puntual en
`src/app/dashboard/classes/__tests__/actions.test.ts`.

**Primer intento (TASK-39af78ac):** abortó correctamente porque yo tenía un
merge a medio resolver (`UU`) en el árbol de trabajo — comportamiento
correcto, no es el bug reportado acá.

**Segundo intento (TASK-1f936d99):** después de que yo abortara mi merge
local (`git merge --abort`, árbol limpio en `staging` @ `ba3c23a`), redespaché
pidiéndole a la Flota que ejecutara ella misma `git merge origin/main`,
resolviera el conflicto documentado en el requerimiento, y dejara un commit
de merge normal. Respuesta:

```
Tarea: TASK-1f936d99
Estado: ✅ Aprobado+validado
Ciclos de revisión: 1
Resumen:
```

(el campo `Resumen` vino vacío, sin texto).

Al revisar la rama resultante `fleet/TASK-1f936d99-mergear-la-rama-origin-main-dentro-de-la`
en el worktree `/vaults/sdelvillar/tennis-app/.fleet-worktrees/app-tennis-TASK-1f936d99-mergear-la-rama-origin-main-dentro-de-la`:

```
$ git log fleet/TASK-1f936d99-... -5 --oneline
ba3c23a TASK-6c3069e8: ...   <- mismo commit que staging antes del ticket
4083f4a TASK-0a44ad5b: ...
...

$ git diff origin/staging fleet/TASK-1f936d99-... --stat
(sin salida — diff vacío)

$ git merge-base --is-ancestor origin/main fleet/TASK-1f936d99-...
exit 1   <- origin/main NO es ancestro, el merge nunca se ejecutó

$ gh pr list --head fleet/TASK-1f936d99-... --state all
(sin salida — no se creó ningún PR)
```

Es decir: la rama del ticket es un puntero al mismo commit que `staging`
tenía antes de despachar. No hubo `git merge`, no hubo commit de resolución
de conflicto, no hubo PR. La Flota no hizo ningún trabajo real, pero igual
reportó `✅ Aprobado+validado`.

## Problema / Causa raíz

No confirmado leyendo el código — es una hipótesis basada en el
comportamiento observado, no una certeza. Posibles causas:

1. El requerimiento pedía una operación de `git merge` con conflicto
   explícito para que el agente la resuelva, en vez del patrón habitual de
   "edita este archivo de código de la aplicación". Es posible que el
   pipeline de planificación/implementación no sepa ejecutar `git merge`
   como parte de la tarea (solo edita archivos vía diff/patch) y por lo
   tanto no hizo nada, pero el gate de validación no detectó "cero cambios"
   como motivo de rechazo.
2. Alternativamente, el agente pudo haber intentado el merge, encontrado
   el conflicto, y en vez de resolverlo devolvió el árbol tal cual estaba
   (sin abortar el merge ni comitear la resolución), y luego el worktree se
   descartó/reseteó antes del commit final — dejando la rama en el punto de
   partida.
3. El `validation_gate` (que en otros requerimientos ya reportados —
   ver `10-validation-gate-exige-suite-completa-no-alcance-del-ticket.md` —
   corre la suite completa) pudo haber corrido contra el estado sin cambios
   (que por definición pasa los mismos tests que ya pasaban) y eso bastó
   para marcar "validado", sin verificar que el diff no esté vacío.

## Fix propuesto

- Agregar una guarda en el pipeline de validación: si el diff de la rama del
  ticket contra su rama base es vacío (`git diff base...HEAD --stat` sin
  salida), rechazar automáticamente con un estado explícito tipo
  `❌ Sin cambios producidos` en vez de dejar que llegue a `✅ Aprobado+validado`.
- Si el requerimiento involucra explícitamente comandos de Git (merge,
  rebase, cherry-pick) más allá de editar archivos de código, considerar si
  el agente Dev actual soporta ese flujo o si hace falta un modo/agente
  distinto para "operaciones de Git" vs "cambios de código en archivos".
  Si no lo soporta, que el rechazo lo diga explícitamente en el resumen
  (ej. "este tipo de requerimiento no está soportado, requiere resolución
  manual") en vez de reportar aprobación falsa.

## Criterios de aceptación

1. Un ticket cuyo diff resultante contra la rama base es vacío nunca debe
   reportar `✅ Aprobado+validado` — debe reportar un estado de rechazo
   explícito indicando "sin cambios".
2. Si se determina que operaciones de Git tipo `merge`/resolución de
   conflictos no están soportadas por el pipeline actual, el mensaje de
   rechazo debe decirlo en texto explícito, no solo fallar en silencio.
3. Test de regresión: despachar un requerimiento que a propósito no requiera
   ningún cambio de código (ej. "no hagas nada, deja el árbol igual") y
   verificar que el resultado sea un rechazo explícito, no una aprobación.
