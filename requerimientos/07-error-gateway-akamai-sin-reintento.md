# Requerimiento: reintentar automáticamente cuando el proveedor del modelo devuelve una página de error HTML (gateway caído) en vez de JSON

**Estado:** ✅ aplicado (2026-07-16)

## Contexto

Despachando `resolver_requerimiento` sobre `recomendador` para un fix chico (hacer obligatorio un campo con express-validator + tests), 3 intentos consecutivos en menos de un minuto devolvieron el mismo resultado exacto:

```
Estado: ⚠️ Requiere revisión (no aprobó o no validó)
Resumen:
<HTML><HEAD><TITLE>Error</TITLE></HEAD><BODY>
An error occurred while processing your request.<p>
Reference #221.245f1cc8.<timestamp>.<hash>
<P>https://errors.edgesuite.net/221.245f1cc8.<timestamp>.<hash></P>
</BODY></HTML>
```

Esto es una página de error de **Akamai** (CDN/edge del proveedor del modelo, probablemente OpenRouter dado el uso de `nvidia/nemotron-3-ultra-550b-a55b:free` visto en `/metrics/history` ese mismo día) — es decir, el proveedor upstream estaba caído o rechazando requests a nivel de edge, no un error de la lógica del pipeline ni del requerimiento.

El pipeline reportó esto como "Requiere revisión" igual que rechazos legítimos de `quality_reviewer` (código malo, validación fallida), sin distinguir "el LLM respondió con contenido inválido para nuestro parser" de "el LLM ni siquiera respondió, el proveedor está caído". Quien despachó no tenía forma de saber, sin inspeccionar el `Resumen` a mano, que reintentar minutos después probablemente funcionaría (de hecho, para el ticket anterior en la misma sesión, un timeout/desconexión similar sí se resolvió solo al reintentar una vez).

## Causa raíz (hipótesis, a confirmar leyendo el código real)

Probablemente en la función que llama al modelo (la misma zona de `dynamic_developer_node` u otra capa de cliente HTTP hacia OpenRouter/MiniMax), la respuesta se trata como "contenido del LLM" sin verificar primero que sea JSON válido con la forma esperada (choices/message/content). Una respuesta HTML de error de Akamai probablemente:
- O bien lanza una excepción de parseo que se captura genéricamente y se reporta como fallo del ciclo (sin reintento), o
- Se cuenta como "0 bloques FILE_BEGIN detectados" y se trata como que el LLM "no generó código", cuando en realidad el LLM nunca fue alcanzado.

## Fix propuesto

1. Detectar este patrón específico (respuesta que empieza con `<HTML>` o cuyo `Content-Type` no es `application/json`, o un status code 5xx/502/503/504 del proveedor) en la capa de llamada al LLM, **antes** de intentar parsearla como contenido de agente.
2. Cuando se detecta, reintentar automáticamente 1-2 veces con backoff corto (ej. 5s, 15s) — igual patrón que ya existe para `npm install` (ver requerimiento 04: reintento único antes de abortar).
3. Si tras los reintentos sigue fallando, reportar el resultado como un estado distinto de "Requiere revisión" (ej. `"provider_error"` o similar) para que quien despacha sepa de inmediato, sin tener que leer el HTML crudo, que el problema es externo y que reintentar más tarde tiene sentido — en vez de asumir que el requerimiento o el código generado fue el problema.

## Criterios de aceptación

1. Una respuesta HTML de error (Akamai u otro CDN) del proveedor del modelo dispara reintento automático (1-2 veces, backoff corto) antes de contarla como fallo del ciclo.
2. Si el reintento tiene éxito, el pipeline continúa normalmente sin que quien despachó note nada distinto de un ciclo normal.
3. Si los reintentos se agotan, el `Resumen` devuelto por `resolver_requerimiento` debe indicar explícitamente "error del proveedor del modelo" (no genérico "Requiere revisión"), para diferenciarlo de un rechazo por calidad de código.
4. Test de regresión: mockear el cliente HTTP del LLM para que devuelva una respuesta HTML tipo Akamai en la primera llamada y una respuesta JSON válida en la segunda; confirmar que el ciclo se completa exitosamente sin contarlo como fallo.

## Verificación

Fix aplicado en `_invoke_chain` (`agile_scripts/langgraph_fleet.py`): nueva función
`_looks_like_gateway_error_page()` detecta contenido HTML de error; si se
detecta, reintenta el MISMO modelo hasta 2 veces con backoff (5s, 15s) antes
de darlo por perdido y pasar al siguiente de la cadena (reutiliza el mecanismo
de fallback ya existente para errores de cuota). Si se agotan todos los
modelos, el `RuntimeError` final menciona explícitamente "Error del proveedor
del modelo" — como no hay try/except alrededor de `_invoke_dev` en
`dynamic_developer_node`, esto se propaga y `fleet_api._run_fleet_worker` lo
captura como `job.status = "error"` con ese mensaje en `job.summary`,
distinto de `"rejected"` (que es el status de un rechazo real de
`quality_reviewer`).

Tests de regresión en `tests/test_quota_error_detection.py`:

```
test_detecta_pagina_de_error_de_gateway_tipo_akamai PASSED
test_invoke_chain_reintenta_tras_pagina_de_error_de_gateway_y_luego_funciona PASSED
test_invoke_chain_agota_reintentos_de_gateway_y_reporta_error_de_proveedor PASSED
```
