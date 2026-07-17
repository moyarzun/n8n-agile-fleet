# Requerimiento: el agente reescribe archivos completos con contenido no solicitado incluso cuando el tamaño coincide, y no coordina cambios entre archivo .ts y su template

**Estado:** ✅ aplicado (2026-07-16)

## Contexto

Distinto del patrón de truncamiento por tamaño (ver requerimiento 06): en `recomendador` se despachó un ticket **muy acotado** — agregar un método `diasParaRecordar()` a `perfil.page.ts` (460 líneas) y un bloque `*ngIf` pequeño en `perfil.page.html` (462 líneas), sin tocar nada más.

Resultado (`TASK-3d43d4cd`, marcado "✅ Aprobado+validado"):

- `perfil.page.ts`: **0 cambios** (idéntico byte a byte al original, confirmado con `diff`). El método pedido nunca se agregó.
- `perfil.page.html`: reescrito completo (462 → 448 líneas, tamaño similar por lo que el guard de tamaño del requerimiento 06 NO lo habría detectado) con una estructura de página totalmente distinta — header (`ion-header`/`ion-toolbar`) que no existía, layout de avatar diferente, clases CSS distintas — pero **sí** contenía, enterrado dentro de esa reescritura, el bloque pedido:
  ```html
  <div class="card-waiting-message" *ngIf="rec.estado === 'pendiente' && !canRecordar(rec)">
    <span class="waiting-text">Podrás enviar un recordatorio en {{ diasParaRecordar(rec) }} {{ diasParaRecordarSingular(rec) ? 'día' : 'días' }}</span>
  </div>
  ```
  Este bloque llama a `diasParaRecordar(rec)` y `diasParaRecordarSingular(rec)` — **ninguno de los dos existe en `perfil.page.ts`** (que no fue tocado). Si esto se hubiera mergeado, la página habría roto en runtime (Angular lanza error de template al no encontrar el método).

`quality_reviewer` aprobó igual. `bin/fleet-validate` de este proyecto no corre `ng build` ni type-checking de templates Angular (solo tests unitarios `node --test` de `api/`), así que no había ningún gate capaz de detectar una llamada a un método inexistente en un template.

Se descartó el resultado sin mergear (se revisó el diff manualmente, como exige `fleet-dispatch/SKILL.md`) y se implementó el cambio a mano en los archivos originales.

## Causa raíz (hipótesis)

1. Igual que en el requerimiento 06: el agente regenera el archivo completo en vez de aplicar un diff dirigido, y el modelo "alucina" una versión distinta de la página en vez de reproducir fielmente el original — esta vez no por límite de tokens (el tamaño coincide), sino simplemente porque el modelo no está anclado a reproducir el contenido exacto, solo a "cumplir el pedido" a su manera.
2. Cuando el ticket requiere tocar dos archivos relacionados (`.ts` + `.html` de un mismo componente), no hay garantía de que el modelo efectivamente modifique ambos de forma consistente — puede escribir solo uno de los dos bloques `FILE_BEGIN` y dejar el otro sin cambios, generando una referencia rota entre ellos.
3. No existe ningún gate de validación para proyectos Angular/frontend en este pipeline: `validation_gate` depende enteramente de `bin/fleet-validate` del workspace, y ese script (al menos en `recomendador`) no ejecuta `ng build` ni cualquier verificación de que los templates referencien símbolos existentes en su componente.

## Fix propuesto

1. **Extender la guarda de tamaño del requerimiento 06** con una comprobación de contenido, no solo de tamaño: si un archivo `.html`/`.ts` existente es reescrito completo y el diff resultante toca más del ~80% de las líneas totales (no solo si se achica), marcarlo como sospechoso también — un ticket que pide "agregar un bloque pequeño" no debería producir un diff que reemplaza casi todo el archivo. Esto es complementario al guard de truncamiento (que solo mira si *se achica*); aquí el archivo no se achica pero cambia casi entero.
2. **Verificación cruzada básica para Angular**: cuando se detecta un par de archivos `X.page.ts` / `X.page.html` (o cualquier componente con selector Angular) entre los archivos tocados en el mismo ciclo, hacer un chequeo liviano (regex, no requiere compilar) de que los métodos/propiedades referenciados en el `.html` vía `{{ nombreMetodo(...) }}` o `*ngIf="nombreMetodo(...)"` existan como método/propiedad en el `.ts` correspondiente. Si el `.html` referencia un símbolo que no aparece en el `.ts` (ni en el original ni en el nuevo), rechazar el ciclo con feedback explícito ("el template usa `diasParaRecordarSingular` pero no existe en el componente").
3. Alternativa más robusta a mediano plazo: si el proyecto es Angular/Ionic (detectable por `angular.json`) y el contenedor tiene Node (ya lo tiene, `node:22-alpine`), agregar un `bin/fleet-validate` de referencia/plantilla que sugiera correr `ng build` como parte del gate — pero esto depende de que cada proyecto lo implemente (ver nota relacionada del requerimiento 06 sobre gates por stack).

## Criterios de aceptación

1. Un ciclo que reescribe >80% de un archivo existente cuando el ticket pedía un cambio puntual se marca como sospechoso (similar a la guarda de truncamiento, pero por "cambio excesivo" en vez de "reducción de tamaño").
2. Cuando se tocan un `.ts` y su `.html` correspondiente (mismo nombre base) en el mismo ciclo, una verificación liviana confirma que los símbolos nuevos referenciados en el template existen en el componente; si no, el ciclo se rechaza con feedback específico señalando el símbolo faltante.
3. Test de regresión: simular una respuesta LLM que solo escribe el `.html` de un par `.ts`/`.html`, con el `.html` referenciando un método nuevo — confirmar que el ciclo se rechaza en vez de aprobarse.
4. Caso negativo: un ticket que legítimamente pide reescribir gran parte de un archivo chico (ej. "rediseña este componente") no debe bloquearse por la guarda de "cambio excesivo" — quizás requiera que el guard 2 (>80%) solo aplique a archivos por encima de cierto tamaño (ej. >100 líneas), igual que el guard de truncamiento del requerimiento 06.

## Nota relacionada

Junto con el requerimiento 06 (truncamiento en archivos grandes) y el 07 (errores de gateway sin reintento), este es el tercer patrón distinto de "aprobado pero incorrecto" encontrado en una sola sesión de trabajo sobre `recomendador`. Sugiere que vale la pena invertir en un chequeo genérico más fuerte antes de `quality_reviewer` — comparar el diff real contra la intención del ticket, no solo contra la salida de tests — en vez de ir parchando caso por caso.

## Verificación

Fix aplicado en `_apply_workspace_changes` (`agile_scripts/langgraph_fleet.py`), extendiendo la guarda del requerimiento 06:

1. **Guarda de reescritura excesiva**: para archivos existentes >100 líneas, se calcula `difflib.SequenceMatcher(...).ratio()` entre el contenido original y el propuesto; si la similaridad es <20% (>80% cambiado), se rechaza — independiente de si el tamaño se achica o no.
2. **Chequeo cruzado Angular .ts/.html**: nuevas funciones `_extract_template_method_calls()` (regex sobre `{{ }}`, `*ngIf`/`*ngFor`, `[binding]`) y `_method_exists_in_ts()`. Si un `.html` referencia un método que no existe ni en el `.ts` nuevo de este ciclo ni en el existente en disco, se rechaza el `.html` con el símbolo faltante explícito en el motivo.

Tests de regresión en `tests/test_anti_truncation_guard.py` (cubren los 4 criterios de aceptación, incluyendo los casos negativos):

```
test_rechaza_reescritura_excesiva_sin_achicamiento PASSED
test_archivo_chico_reescrito_casi_entero_no_activa_guarda_de_reescritura PASSED
test_rechaza_html_angular_que_referencia_metodo_inexistente_en_ts PASSED
test_no_rechaza_html_angular_cuando_el_metodo_existe_en_el_ts PASSED
```

Nota de alcance: no se implementó la alternativa de "agregar `ng build` a un `bin/fleet-validate` de referencia" — queda como mejora futura dependiente de cada proyecto, tal como el propio requerimiento la marca como no bloqueante.
