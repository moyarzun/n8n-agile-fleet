# Requerimiento: la guarda "fuera de alcance" (req. 16) no reconoce paréntesis literales en rutas de archivo — rechaza el único archivo mencionado en el ticket

**Estado:** 🔴 abierto

## Contexto

Durante el plan `mobile-cleanup` (Plan 7), tras completar con éxito la Tarea 3 parcialmente (creó `mobile/lib/format.ts` y su test, pero no logró editar los 3 archivos consumidores en 6 ciclos — probablemente por tamaño/complejidad, no relacionado con este hallazgo), se despachó un ticket de fix acotado a un solo archivo:

> "Alcance permitido: SOLO `mobile/app/(tabs)/index.tsx` (el path del archivo tiene paréntesis literales en el nombre de carpeta "(tabs)", tenlo en cuenta al escribir la ruta exacta)."

`job_id` del ticket: `TASK-80de0a0a`. El job terminó `⚠️ Requiere revisión` en los 6 ciclos con este mensaje:

```
mobile/app/(tabs)/index.tsx: archivo existente fuera del alcance del ticket
(no mencionado en el requerimiento y fuera del árbol de directorios de los
archivos que sí se mencionaron) — rechazado por guarda de alcance
```

## Problema

El archivo rechazado por "no mencionado" es **el único archivo que el `requerimiento` menciona** — aparece literalmente en la primera línea del alcance permitido, y se repite varias veces más adelante en el texto (en el criterio de aceptación, en la descripción del bloque a eliminar). Esto es análogo al hallazgo del req. 16 (donde `codebase_reader`/las guardas de alcance no capturaban rutas dinámicas con corchetes `[conversationId]` hasta que se extendió la regex de extracción de paths para incluir `[]`), pero esta vez el carácter especial no reconocido es el paréntesis `()` — usado por convención en Next.js App Router y Expo Router para "route groups" (carpetas como `(tabs)`, `(auth)`, etc. que no forman parte de la URL pero sí del path de archivo real).

Es muy probable que la guarda `_is_out_of_scope` (o el regex de extracción de paths que comparte con `_find_explicitly_forbidden_files`, ambas del req. 15/16) tenga una clase de caracteres que ya incluye `[`/`]` (agregados en el fix del req. 16) pero no incluye `(`/`)`, y por eso al intentar extraer "los paths mencionados en el requerimiento" para construir el árbol de alcance, el regex corta la ruta en el paréntesis (tratándolo como el fin del path, o como un carácter no válido en un path), y termina sin reconocer ninguna coincidencia con `mobile/app/(tabs)/index.tsx` tal como aparece realmente en el árbol de archivos.

## Investigación sugerida

1. Ubicar el mismo regex de extracción de rutas de archivo que ya se corrigió en el req. 16 para incluir `[` y `]` (usado tanto por `_is_out_of_scope` como por `_find_explicitly_forbidden_files` y por `codebase_reader` para capturar contexto), y agregar también `(` y `)` a la clase de caracteres válidos dentro de un segmento de path — cubre "route groups" de Next.js/Expo Router, un patrón común en cualquier proyecto con App Router o Expo Router (no es específico de este proyecto).
2. Confirmar que `codebase_reader` tampoco tiene problemas para LEER el contenido de archivos con paréntesis en el path como contexto (esto ya podría estar bien, dado que en despachos anteriores de este mismo plan sí se leyó `mobile/app/(tabs)/index.tsx` como parte del contexto capturado — si el problema es solo en la guarda de alcance y no en la lectura de contexto, acotar el fix a esa guarda específica).

## Criterios de aceptación

1. Un `requerimiento` que menciona un archivo cuyo path contiene paréntesis literales (ej. `mobile/app/(tabs)/index.tsx`) no debe ser rechazado por la guarda de "fuera de alcance" cuando el ticket efectivamente escribe en ese archivo.
2. Test de regresión que reproduzca el caso exacto: un requerimiento que menciona `mobile/app/(tabs)/index.tsx` como único archivo en alcance, y un ticket que escribe en ese archivo → no debe rechazarse por la guarda de alcance.

## Nota sobre severidad

Media-alta: bloquea cualquier ticket futuro sobre archivos dentro de "route groups" de Next.js (`src/app/(marketing)/...`, `src/app/(dashboard)/...` si este proyecto llegara a usarlos) o de Expo Router (`mobile/app/(tabs)/...`, ya en uso activo hoy) — un patrón de organización de carpetas común, no una rareza de este proyecto. No se mergeó nada de este ticket. Redespacho pendiente con un workaround (mencionar el path de más formas distintas, o evaluar si dividir la tarea evitando parear la ruta completa ayuda) mientras se corrige.
