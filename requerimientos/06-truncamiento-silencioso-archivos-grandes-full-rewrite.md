# Requerimiento: `_apply_workspace_changes` debe detectar y rechazar truncamientos masivos al reescribir un archivo existente grande

**Estado:** ✅ aplicado (2026-07-16)

## Contexto

Despachando `resolver_requerimiento` sobre `recomendador` (`/vaults/sdelvillar/recomendador`) para un fix **mecánico y acotado**: reemplazar 16 ocurrencias exactas de `''` por `\'` dentro de `back/config/Migrations/20260625120000_ImportEmpresasObjetivoLatamV2.php`, un archivo de datos de **4156 líneas** (array PHP con ~4000 tuplas `[nombre, pais, rubro]`).

Dos despachos consecutivos sobre el mismo archivo (`TASK-797d15ac` y `TASK-f5ecc032`), el segundo con instrucciones EXPLÍCITAS de "PROHIBIDO REESCRIBIR EL ARCHIVO... usa sed o equivalente... el archivo debe seguir teniendo prácticamente el mismo número de líneas", terminaron ambos en el mismo resultado: el agente `PHP` devolvió el archivo completo reescrito y **truncado**:

- Intento 1: 4156 → 1220 líneas. Cambió `use Migrations\AbstractMigration` por `use Phinx\Migration\AbstractMigration` (namespace incorrecto para este proyecto CakePHP), `up()` por `change()`, y **redefinió la PK de `empresas_objetivo` como `uuid`** en vez de `integer` — habría roto la FK `servicios_empresas_objetivo.empresa_objetivo_id` (tipo `integer`) de haberse mergeado.
- Intento 2 (con las reglas estrictas arriba): 4156 → 1345 líneas. Volvió a reescribir desde cero, esta vez borrando también `declare(strict_types=1);`.

Ambos jobs terminaron con `quality_reviewer` marcando **"✅ Aprobado+validado"** — el reviewer no detectó el truncamiento porque `bin/fleet-validate` del proyecto (comando de validación determinista del workspace) solo corre tests de `api/` (Node); no hay ninguna validación de `back/` (PHP) en este entorno (el contenedor `node:22-alpine` no tiene `php`), así que nada dentro del pipeline comparó el archivo resultante contra el original.

Se evitó el daño real solo porque quien despachó revisó el diff manualmente antes de mergear (ver regla en `fleet-dispatch/SKILL.md`: nunca confiar en "approved" sin revisar el diff). Sin esa verificación manual, se habría mergeado una migración que **pierde ~2800 líneas de datos** (cientos de empresas objetivo) y **rompe el tipo de la PK**.

## Causa raíz

1. `dynamic_developer_node` (línea ~1163) siempre pide al LLM que devuelva el **archivo completo** dentro de `===FILE_BEGIN/END===`, sin mecanismo de diff/patch parcial.
2. El modelo del agente `PHP` (ver selección de modelo en la función de arriba de la línea 100, con fallback a MiniMax) tiene `max_tokens=40960` en la llamada principal (línea 66/86). Un archivo de 4156 líneas con estructura repetitiva de tuplas probablemente excede ese presupuesto de tokens de salida al tener que reproducirlo íntegro carácter por carácter — el modelo no falla ni trunca con error, sino que **reescribe una versión "resumida"/regenerada** del archivo (alucinando una estructura distinta) que sí entra en el límite.
3. `_apply_workspace_changes` (línea 400-435) escribe el contenido devuelto **sin ninguna validación de tamaño**: no compara `len(content)` / número de líneas del archivo nuevo contra el archivo existente antes de sobreescribirlo con `os.unlink` + `open(..., "w")`.
4. `regression_guard` y `validation_gate` tampoco detectan esto: `regression_guard` solo revisa un set acotado de archivos "sensibles" (no quedó claro si esta migración entra en esa categoría, pero en ambos intentos reportó "✓ sin regresiones"), y `validation_gate` depende de `bin/fleet-validate` del proyecto, que en este caso no cubre PHP.

## Fix propuesto

En `_apply_workspace_changes` (o justo antes de aplicar cada bloque), agregar una guarda de tamaño quando el archivo **ya existe** en el workspace:

```python
def _apply_workspace_changes(workspace: str, llm_response: str) -> list:
    applied = []
    rejected = []
    pattern = r"===FILE_BEGIN:\s*([^\n\r=]+?)===[ \t]*\r?\n?(.*?)===FILE_END==="
    matches = _re.findall(pattern, llm_response, _re.DOTALL)
    for rel_path, content in matches:
        rel_path = rel_path.strip()
        # ... (normalización de ruta existente, sin cambios) ...
        full_path = os.path.join(workspace, rel_path)

        # --- NUEVO: guarda anti-truncamiento ---
        if os.path.exists(full_path):
            try:
                with open(full_path, "r", errors="ignore") as fh:
                    original_lines = sum(1 for _ in fh)
                original_size = os.path.getsize(full_path)
            except OSError:
                original_lines = original_size = 0
            new_lines = content.count("\n") + 1
            new_size = len(content.encode("utf-8", errors="ignore"))
            # Umbral: si el archivo original es "grande" (>500 líneas o >20KB)
            # y el nuevo contenido pierde más del 30% de líneas o tamaño,
            # es sospechoso de truncamiento/reescritura no solicitada.
            is_large = original_lines > 500 or original_size > 20_000
            shrank_a_lot = (
                original_lines > 0 and new_lines < original_lines * 0.7
            ) or (
                original_size > 0 and new_size < original_size * 0.7
            )
            if is_large and shrank_a_lot:
                rejected.append(
                    f"{rel_path}: original {original_lines} líneas/{original_size}B "
                    f"→ propuesto {new_lines} líneas/{new_size}B "
                    f"(pérdida >30%, rechazado por guarda anti-truncamiento)"
                )
                logger.warning(
                    "Archivo %s rechazado: encogió de %d a %d líneas (posible "
                    "truncamiento/reescritura no solicitada del LLM)",
                    full_path, original_lines, new_lines,
                )
                continue  # no escribir este archivo

        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        if os.path.exists(full_path):
            try:
                os.unlink(full_path)
            except OSError:
                pass
        with open(full_path, "w") as fh:
            fh.write(content)
        applied.append(rel_path)
        logger.info("Archivo escrito: %s", full_path)

    if rejected:
        logger.warning("Archivos rechazados por guarda anti-truncamiento: %s", rejected)
    return applied, rejected  # el caller debe propagar `rejected` al reviewer_feedback
```

El caller (`dynamic_developer_node`) debe tratar `rejected` como una razón de **rechazo automático del ciclo** (igual que "no generó bloques FILE_BEGIN/END válidos", línea ~1377-1380), con feedback explícito al agente:

```
"El archivo {rel_path} fue rechazado porque el contenido generado tiene
{new_lines} líneas vs. las {original_lines} originales (pérdida >30%).
Para archivos grandes NO regeneres el archivo completo: usa un enfoque de
edición dirigida (ej. describe los cambios línea por línea) o, si el
archivo es demasiado grande para reproducirlo íntegro, divide el trabajo
en ediciones más pequeñas."
```

## Alcance / alternativas consideradas

- **Alternativa más robusta (no implementada aquí):** dar al agente developer una herramienta de "patch"/"edit" real (tipo `str_replace` acotado) en vez de forzarlo siempre a devolver el archivo completo. Esto resolvería el problema de raíz (el modelo nunca necesitaría reproducir 4000 líneas para cambiar 16), pero es un cambio de arquitectura más grande que el guard de tamaño propuesto arriba. Vale la pena evaluarlo como mejora futura si este patrón se repite con archivos grandes.
- El guard de tamaño propuesto es una mitigación barata y rápida de aplicar que como mínimo evita mergear silenciosamente una pérdida masiva de datos, aunque no resuelve el caso base (el ticket seguiría sin poder completarse automáticamente para archivos grandes).

## Criterios de aceptación

1. Si el LLM devuelve un archivo existente con >30% menos líneas/bytes que el original, y el original es "grande" (>500 líneas o >20KB), el archivo **no se escribe** y se cuenta como fallo del ciclo (no como éxito silencioso).
2. El feedback de rechazo llega al agente en el siguiente ciclo (`reviewer_feedback` o equivalente), mencionando explícitamente el problema de truncamiento y sugiriendo edición dirigida en vez de regeneración completa.
3. `quality_reviewer` NO debe poder marcar como "✅ Aprobado" un ciclo donde `rejected` no está vacío para ningún archivo.
4. Test de regresión: mockear una respuesta LLM que reescribe un archivo de 4000 líneas simuladas a 1000 líneas; confirmar que `_apply_workspace_changes` no escribe el archivo y retorna el path en `rejected`.
5. Caso negativo: un archivo nuevo (que no existe antes) o un archivo existente pequeño (<500 líneas y <20KB) NO debe activar el guard aunque cambie mucho de tamaño (evitar falsos positivos en refactors legítimos de archivos chicos).

## Nota relacionada

Este mismo despacho también reveló que `bin/fleet-validate` en `recomendador` no cubre `back/` (PHP) porque el contenedor de la flota (`node:22-alpine`) no tiene `php` instalado. Eso es un problema de *ese* proyecto (ya hay un fix local: excluir integración de DB, correr solo unit tests de Node), no de la flota en sí — pero refuerza que para proyectos multi-stack (Node + PHP + Angular en el mismo repo) el gate determinista actual solo cubre una parte del stack, y una regresión en el resto (como este truncamiento en PHP) puede pasar sin que ningún test la atrape. La guarda de tamaño propuesta arriba es independiente del stack y protegería este caso sin necesitar php en el contenedor.

## Verificación

Fix aplicado literalmente como se propuso en `agile_scripts/langgraph_fleet.py`:

- `_apply_workspace_changes` ahora devuelve `(applied, rejected)` en vez de solo `applied`, con la guarda de tamaño (>500 líneas o >20KB de original, >30% de pérdida).
- `dynamic_developer_node` acumula `all_rejected` entre agentes/ciclos y lo expone como `rejected_files` en el estado.
- `reviewer_node` (quality_reviewer) hace fast-reject **antes** de llamar al LLM si `rejected_files` no está vacío — nunca puede aprobar un ciclo con truncamiento detectado (criterio de aceptación 3).
- Se agregó `rejected_files: List[str]` a `FleetState`.

Tests de regresión en `tests/test_anti_truncation_guard.py` (cubren los 5 criterios de aceptación):

```
test_rechaza_reescritura_masiva_de_archivo_grande_existente PASSED
test_archivo_nuevo_no_activa_la_guarda_aunque_sea_chico PASSED
test_archivo_existente_pequeno_no_activa_la_guarda_aunque_encoja_mucho PASSED
test_archivo_grande_que_no_encoge_mucho_se_escribe_normalmente PASSED
test_reviewer_node_rechaza_rapido_si_hay_archivos_rechazados PASSED
```

Nota de alcance: no se implementó la alternativa de herramienta de "patch"/edit
real (sección "Alcance / alternativas consideradas") — sigue siendo la mejora
de arquitectura más grande, evaluada pero no aplicada acá, tal como el propio
requerimiento la marca como no bloqueante.
