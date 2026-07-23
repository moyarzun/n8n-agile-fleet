# Requerimiento: la guarda de exclusión explícita bloquea el archivo NUEVO que el ticket pide crear, confundiéndolo con los que pide NO tocar

**Estado:** ✅ implementado (2026-07-23) — `_find_explicitly_forbidden_files` excluye rutas declaradas como "archivo nuevo a crear" (`_find_declared_creation_targets`, `agile_scripts/langgraph_fleet.py`). Tests: `tests/test_new_file_target_vs_explicit_exclusion.py`.

## Contexto

Variante del mismo mecanismo ya reportado en `17-guarda-de-exclusion-explicita-falso-positivo-con-frase-fuera-de.md`
(`_find_explicitly_forbidden_files`), pero con una redacción distinta que
dispara el mismo tipo de falso positivo sin usar "fuera de".

Proyecto: `app-tennis`. Ticket `TASK-e283fd8f`, workspace
`/vaults/sdelvillar/tennis-app/app-tennis`. Requerimiento pedía **crear** un
archivo nuevo, `.github/workflows/mobile-tests.yml`, y contenía esta frase de
alcance (deny-list simple, sin inversión "fuera de"):

> "Alcance: SOLO crear el archivo nuevo `.github/workflows/mobile-tests.yml`.
> No modificar `quick-checks.yml`, `test-battery.yml`, ni ningún otro workflow
> existente."

Resultado tras 6 ciclos:

```
Estado: ⚠️ Requiere revisión (no aprobó o no validó)
Uno o más archivos fueron rechazados porque el contenido generado perdió
más del 30% de sus líneas/bytes respecto al original...
.github/workflows/mobile-tests.yml: el requerimiento prohíbe explícitamente
modificar este archivo ('no modifiques/no toques …') — rechazado por guarda
de exclusión explícita
```

## Problema

El archivo bloqueado (`mobile-tests.yml`) es exactamente el archivo que el
ticket pedía **crear**, no uno de los mencionados en la lista de "no tocar"
(`quick-checks.yml`, `test-battery.yml`). El regex de
`_find_explicitly_forbidden_files` parece capturar el path equivocado —
posiblemente matcheando de forma demasiado amplia sobre el directorio/patrón
(`.github/workflows/*.yml` o la palabra "workflow") en vez de anclarse a los
nombres de archivo literales listados después de "No modificar". Al no
distinguir "no tocar A y B" de "el archivo que sí hay que tocar/crear es C",
termina agregando C (el objetivo real del ticket) a la lista de prohibidos.

A diferencia del caso #17 (inversión semántica por "fuera de"), acá no hay
ninguna palabra de inversión — es un fallo de anclaje/alcance del regex sobre
qué token exacto cuenta como "el archivo prohibido" cuando la oración
menciona múltiples archivos `.yml` en la misma frase (los prohibidos Y,
en otra parte del mismo párrafo, el que se pide crear).

## Consecuencia
Igual que en #17: rechazo determinístico en el 100% de los 6 ciclos,
ningún archivo llegó a commitearse — el ticket completo quedó inservible,
sin ninguna posibilidad de que el worker lo resuelva solo (el archivo
objetivo está bloqueado en todos los intentos).

## Fix propuesto
1. Cuando el mismo párrafo de alcance menciona explícitamente "el archivo
   nuevo/objetivo es X" (o equivalente, "SOLO crear el archivo nuevo X")
   Y por separado una lista de "no modificar A, B", el extractor de
   prohibidos debería excluir X de la lista de prohibidos aunque comparta
   extensión/directorio con A o B — dar prioridad al archivo declarado como
   objetivo del ticket sobre cualquier match genérico de patrón.
2. Más robusto: anclar el regex de "no modifiques/no toques" a nombres de
   archivo exactos (los tokens que siguen inmediatamente al verbo, separados
   por comas/"ni"), no a un patrón de directorio/extensión compartido con
   otros archivos mencionados en el mismo requerimiento.

## Criterios de aceptación
1. Un ticket que pide crear un archivo nuevo bajo el mismo directorio que
   otros archivos explícitamente excluidos de edición no debe bloquear la
   creación del archivo nuevo.
2. Test de regresión: replicar este ticket exacto (crear `X.yml` nuevo,
   prohibir tocar `Y.yml` y `Z.yml` ya existentes, los 3 en el mismo
   directorio) y confirmar que `X.yml` se crea sin rechazo.
