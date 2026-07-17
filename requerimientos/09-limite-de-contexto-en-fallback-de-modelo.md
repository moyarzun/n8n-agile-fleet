# Requerimiento: un modelo del chain de fallback tiene ventana de contexto insuficiente para archivos de test grandes

**Estado:** ✅ aplicado (2026-07-16)

## Contexto

Al despachar un fix puntual sobre 6-7 archivos de test ya existentes (algunos de ~200-300 líneas cada uno, generados en un despacho previo), dos intentos consecutivos fallaron con:

```
Error code: 400 - {'message': "This endpoint's maximum context length is 65536 tokens.
However, you requested about 76822 tokens (35862 of text input, 40960 in the output)."}
```

Recortar drásticamente el texto del `requerimiento` (de ~1300 a ~500 palabras) **no cambió casi nada** el conteo de tokens de entrada (36410 → 35862) — confirma que el grueso del contexto no viene del `requerimiento` en sí, sino del contenido de los archivos que el pipeline necesita leer/reescribir (`codebase_reader` + los archivos objetivo completos que `dynamic_developer` necesita ver para hacer ediciones quirúrgicas).

## Problema

Algún modelo de la cadena de fallback (`_OR_DEV_CHAIN` en `agile_scripts/langgraph_fleet.py`, no confirmado cuál exactamente — el error no identifica el proveedor) tiene una ventana de contexto de solo 65536 tokens, y `max_tokens` para la salida está fijado en 40960 (ver `_minimax_dev`/modelos equivalentes, línea ~58 en adelante). Con `max_tokens=40960` reservado para la salida, solo quedan ~24500 tokens para todo el input — insuficiente cuando hay que cargar varios archivos de test de 200-300 líneas más el resto del contexto del proyecto.

## Investigación sugerida

1. Identificar qué modelo específico de `_OR_DEV_CHAIN` tiene la ventana de 65536 tokens (revisar la lista de modelos y sus context windows documentados).
2. Evaluar si `max_tokens=40960` es necesario para ese modelo en particular, o si puede reducirse dinámicamente según el modelo (cada modelo de la cadena podría tener su propio `max_tokens` ajustado a su ventana real, en vez de un valor fijo compartido).
3. Alternativa: si el modelo con ventana chica falla por esto, que `_invoke_chain` lo trate como error de capacidad (agregar el mensaje "maximum context length" a las palabras clave de `_is_quota_error`, junto al fix del requerimiento 05) y avance al siguiente de la cadena en vez de fallar el ciclo completo.
4. Para el caso general (no solo este bug puntual): si un ticket requiere editar muchos archivos grandes a la vez, considerar que `dynamic_developer` divida el trabajo en sub-lotes más chicos en vez de intentar generar todo en una sola respuesta del modelo.

## Workaround aplicado mientras tanto

Se dividió manualmente el requerimiento en 2 despachos más chicos (menos archivos por despacho) para mantenerse bajo el límite de contexto de ese modelo específico. No es una solución de fondo — cualquier ticket futuro que necesite tocar varios archivos grandes de una vez puede volver a toparse con esto.

## Criterios de aceptación

1. Un despacho que edite 6-7 archivos de test de 200-300 líneas cada uno no debe fallar por límite de contexto — ya sea evitando el modelo problemático de la cadena, ajustando su `max_tokens`, o tratando el error como recuperable con fallback al siguiente modelo.
2. Si de todas formas hay un límite real de tamaño de tarea, el sistema debe reportarlo de forma clara y accionable (ej. "esta tarea es demasiado grande para un solo ciclo, dividí el requerimiento en partes más chicas") en vez de solo el error crudo del proveedor.

## Verificación

Implementada la alternativa sugerida en investigación punto 3: se agregaron
`"maximum context length"`, `"context length"` y `"context_length_exceeded"`
a las keywords de `_is_quota_error()`. Con esto, cuando un modelo de
`_OR_DEV_CHAIN` devuelve este error, `_invoke_chain` lo trata igual que un
error de cuota/capacidad y avanza automáticamente al siguiente modelo de la
cadena, en vez de fallar el ciclo completo.

Test de regresión en `tests/test_quota_error_detection.py`:

```
test_detecta_error_de_limite_de_contexto_como_error_de_cuota PASSED
```

No se implementaron los puntos 1 (identificar el modelo exacto con ventana
de 65536 tokens) ni 2 (`max_tokens` dinámico por modelo) — quedan como
mejora futura si el fallback automático no fuera suficiente en la práctica
(ej. si TODOS los modelos de la cadena comparten ventanas chicas). El punto
4 (dividir el trabajo en sub-lotes) tampoco se implementó — es un cambio de
arquitectura más grande, fuera de alcance de este fix puntual.
