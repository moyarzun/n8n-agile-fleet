# Requerimiento: `_is_quota_error` no reconoce el formato de error de Nvidia, causando fallos fatales evitables

**Estado:** ✅ aplicado (2026-07-16)

## Contexto

Dos despachos reales sobre `app-tennis` (`TASK-c85cf500` y `TASK-ee412dcb`) terminaron en `status: error` (no `rejected`, no `approved`) con el mismo mensaje:

```json
{"message": "Upstream error from Nvidia: ResourceExhausted: Worker local total request limit reached (34/32)", "code": 502}
```

Este es exactamente el tipo de error que el sistema **ya sabe manejar** — hay un mecanismo de fallback entre modelos (`_invoke_chain`, línea ~267 de `agile_scripts/langgraph_fleet.py`) que prueba una cadena de proveedores (Qwen vía OpenRouter → otros → MiniMax como último recurso) cuando detecta un error de cuota/capacidad vía `_is_quota_error()`.

## Problema

```python
def _is_quota_error(exc: Exception) -> bool:
    if isinstance(exc, RateLimitError):
        return True
    if isinstance(exc, APIStatusError) and exc.status_code in (402, 403, 429, 500, 502, 503, 529):
        return True
    msg = str(exc).lower()
    return any(kw in msg for kw in ("429", "rate limit", "quota", "overload", "529", "capacity", "unavailable"))
```

El código HTTP 502 sí está cubierto por el segundo `if` — pero solo si la excepción es una instancia real de `APIStatusError` con ese `status_code`. Si el error llega como una excepción genérica (no `APIStatusError`) envuelta con el texto `"Upstream error from Nvidia: ResourceExhausted: Worker local total request limit reached"`, ninguna palabra clave de la lista (`429, rate limit, quota, overload, 529, capacity, unavailable`) aparece en ese mensaje — ni "resourceexhausted" ni "request limit" están cubiertas. El resultado: el error NO se clasifica como error de cuota, no dispara el fallback a otro modelo/proveedor de la cadena, y se propaga como fallo fatal del job completo.

## Fix propuesto

Agregar las variantes de mensaje observadas a la lista de palabras clave:

```python
    msg = str(exc).lower()
    return any(kw in msg for kw in (
        "429", "rate limit", "quota", "overload", "529", "capacity", "unavailable",
        "resourceexhausted", "resource exhausted", "request limit", "worker local total request limit",
    ))
```

## Criterios de aceptación

1. Un test unitario que pase una excepción/string con el mensaje real observado (`"Upstream error from Nvidia: ResourceExhausted: Worker local total request limit reached (34/32)"`) a `_is_quota_error()` y confirme que retorna `True`.
2. Confirmar que `_invoke_chain` efectivamente avanza al siguiente modelo de la cadena cuando este tipo de error ocurre (no solo que `_is_quota_error` lo detecte — verificar el flujo completo con un mock que lance este error en el primer modelo y confirme que se llama al segundo).
3. Idealmente, no hardcodear más frases textuales de proveedores específicos a futuro — considerar un chequeo más robusto (ej. buscar substrings de las clases de error comunes entre proveedores: `resourceexhausted`, `insufficient_quota`, `too many requests`, sin depender de que cada proveedor use exactamente las mismas palabras). No es bloqueante para este fix puntual, pero vale la pena si se repite con otro proveedor en el futuro.

## Nota

Esto no garantiza que el job hubiera tenido éxito de todas formas (podría haber agotado también el resto de la cadena de fallback si el sistema entero estaba saturado — había varios otros jobs `fleet/TASK-...` corriendo en paralelo para otro proyecto al momento de estas dos fallas). Pero al menos evitaría que el job muera de forma fatal en el primer proveedor saturado sin intentar los demás.

## Verificación

Fix aplicado literalmente como se propuso: keywords agregadas a `_is_quota_error`
en `agile_scripts/langgraph_fleet.py`.

Tests de regresión en `tests/test_quota_error_detection.py` (criterios 1 y 2):

```
test_detecta_mensaje_real_de_nvidia_como_error_de_cuota PASSED
test_invoke_chain_avanza_al_siguiente_modelo_con_error_de_nvidia PASSED
test_invoke_chain_no_atrapa_errores_que_no_son_de_cuota PASSED
```

El primero pasa el mensaje real observado (`"Upstream error from Nvidia:
ResourceExhausted: Worker local total request limit reached (34/32)"`) y
confirma `True`. El segundo verifica el flujo completo de `_invoke_chain`
con un modelo fake que lanza ese error y confirma que avanza al siguiente
de la cadena. El tercero (no pedido explícitamente, agregado como guardrail)
confirma que errores genuinamente no relacionados con cuota siguen
propagándose sin activar el fallback.

Nota de alcance: no se implementó el criterio 3 (chequeo más robusto y
agnóstico de proveedor) — queda como mejora futura, no bloqueante, tal como
lo marca el propio requerimiento.
