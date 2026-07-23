# Requerimiento: la guarda de regresión bloquea la eliminación intencional de código muerto (exports)

**Estado:** ✅ implementado (2026-07-23) — `_check_ts_exports_regression` excluye exports declarados como eliminación intencional (`_find_intentionally_removed_exports`, `agile_scripts/langgraph_fleet.py`). Tests: `tests/test_regression_guard_intentional_deletion.py`.

## Contexto

Proyecto: `app-tennis` (workspace `/vaults/sdelvillar/tennis-app/app-tennis`), rama base
`reconcile/shared-coach-access`.

Despaché un ticket (`TASK-d11c44eb`) cuyo pedido era explícitamente **borrar** una
función muerta: `markAttendanceForClass` en `src/server/classes/class-service.ts`
ya no tenía ningún caller (confirmado con grep antes de despachar — un merge
anterior había reemplazado su único consumidor por otra función), y el
requerimiento pedía borrarla junto con su helper `assertCanMarkAttendance` si
tampoco tenía otros usos, más el bloque de tests correspondiente.

Resultado tras 6 ciclos de revisión:

```
Estado: ⚠️ Requiere revisión (no aprobó o no validó)
Ciclos de revisión: 6

Resumen:
La validación determinista FALLÓ. Corrige EXACTAMENTE estos errores antes de cualquier otra cosa:

REGRESIÓN DETECTADA — el código generado eliminó elementos existentes:
  - src/server/classes/class-service.ts: exports eliminados: ['assertCanMarkAttendance', 'markAttendanceForClass']

Archivos RESTAURADOS a su contenido previo a este ciclo (la versión con
regresión NO quedó en disco): src/server/classes/class-service.ts
```

El propio guard revirtió el archivo en cada uno de los 6 ciclos, porque el
pedido explícito (eliminar 2 exports específicos) es indistinguible, para el
guard, de una corrupción accidental que elimina exports sin querer.

## Problema / Causa raíz

No confirmado leyendo el código del guard — hipótesis basada en el
comportamiento observado. El guard de "regresión por exports eliminados"
(mencionado también en contexto de otros requerimientos de esta carpeta,
ej. `12-guardia-de-reescritura-excesiva-no-detecta-todos-los-casos.md` y
`13-guardia-reescritura-bloquea-simplificacion-intencional.md`, que documentan
guardas hermanas con el mismo problema de fondo) parece comparar el conjunto
de exports del archivo antes/después del diff generado, y si el conjunto
"después" es un subconjunto estricto del "antes" (algo desapareció), lo trata
como regresión y revierte — sin ningún mecanismo para que el propio
requerimiento declare "esta eliminación es intencional".

Esto bloquea una clase entera de trabajo legítimo: cualquier ticket cuyo
objetivo sea eliminar código muerto, refactorizar consolidando funciones, o
remover una función que un ticket anterior dejó huérfana (exactamente el caso
acá) queda estructuralmente imposible de completar vía la Flota.

## Fix propuesto

- Si el requerimiento (el texto que se le pasa a `resolver_requerimiento`)
  menciona explícitamente palabras como "eliminar", "borrar", "remove",
  "delete" junto con el nombre exacto de la función/export a eliminar, el
  guard debería permitir que ESOS exports específicos desaparezcan sin
  contarlos como regresión — comparando la lista de exports "esperados
  eliminados" (parseada del requerimiento o pasada como metadata explícita)
  contra los que realmente desaparecieron, y solo revertir si desaparece algo
  que NO estaba en esa lista.
- Alternativa más simple: agregar un flag/campo explícito al requerimiento
  (ej. `elementos_a_eliminar: ["markAttendanceForClass", "assertCanMarkAttendance"]`)
  que el orquestador pase como contexto al guard de regresión para que lo
  excluya del chequeo.
- Si ninguna de las dos es viable a corto plazo, que el mensaje de rechazo lo
  diga explícitamente ("este guard no soporta eliminación intencional de
  exports, pedir la eliminación manualmente") en vez de reintentar 6 ciclos
  sin poder converger nunca.

## Criterios de aceptación

1. Un ticket que pide explícitamente eliminar una función/export nombrada no
   debe ser rechazado por el guard de regresión cuando esa función es
   exactamente la que desaparece (y nada más desaparece sin haber sido
   pedido).
2. Si aparece una eliminación NO pedida además de la pedida, el guard debe
   seguir rechazando — el fix es agregar una excepción dirigida, no
   desactivar el guard en general.
3. Test de regresión: un ticket "elimina la función X (sin otros usos) de
   archivo Y" debe poder aprobarse en 1-2 ciclos, no agotar el máximo de
   ciclos sin converger nunca.
