# Metodología de la Flota de Agentes — v3 (TDD + QA Expert)

> Rediseño tras una sesión real donde la Flota generó código que **no parseaba, no
> booteaba y nunca se ejecutó**, obligando a un humano a reescribirlo. Este documento
> explica qué falló, los principios nuevos, y cómo el pipeline los implementa.
>
> **v3 (2026-07-01):** Incorpora TDD como metodología obligatoria, agente experto QA,
> ejecución de Vitest en el gate determinista y `bin/fleet-validate` por proyecto.

---

## 1. Diagnóstico: por qué la v1 producía trabajo inservible

| Síntoma observado en producción | Causa raíz en la v1 |
|---|---|
| Código que no parseaba (comillas escapadas, dobles backslashes) | El "reviewer" era un **LLM leyendo archivos truncados**; nunca se corría `ruby -c`. |
| App no booteaba (initializers de gemas inexistentes, `enum` sin columna → "Undeclared attribute type") | **No había boot check** ni `zeitwerk:check`. |
| Modelo, tabla y spec con **3 diseños distintos** (inglés/español, columnas inexistentes) | El dev generaba modelos **sin anclarse** al spec/factory/esquema reales. |
| Commits directos en `main`, trabajo no revertible, ramas alucinadas | **No había git**: el dev escribía archivos sueltos al workspace. |
| "Aprobado" pero los tests fallaban | El quality gate era **opinión de un LLM**, sin ejecución determinista. |
| Respuestas truncadas / placeholders `# TODO` | Un **único dump monolítico** de "todos los archivos" en una sola llamada. |
| Código sin cobertura de tests | **No había mandato de TDD** ni gate que lo exigiera. |

**Conclusión v1→v2:** ningún ajuste de prompt arregla esto. Falta *ejecutar el código* y
*aislar el trabajo en git*.

**Conclusión v2→v3:** la validación determinista pasaba tsc pero **no ejecutaba tests**.
Un cambio que compila sin tests no tiene definición de hecho real. La v3 incorpora
TDD obligatorio y ejecución de vitest/rspec en el gate.

---

## 2. Principios

1. **El código se ejecuta, no se "lee".** Ningún cambio avanza sin pasar un gate
   determinista (sintaxis + boot + **tests unitarios/integración**). Los errores
   reales —no opiniones— se realimentan al desarrollador.
2. **TDD es obligatorio.** El developer escribe los tests *antes o junto con* el código
   (Red → Green → Refactor). Un cambio sin tests = gate falla = ciclo de corrección.
3. **La fuente de verdad ya existe.** Para Rails: el SPEC manda; modelo, migración y
   factory se alinean a él (triangulación spec↔factory↔esquema). Prohibido inventar.
4. **Trabajo aislado y revertible.** Cada ticket vive en su rama `fleet/<ticket>-<slug>`
   con commit + PR. **Nunca** se toca `main`.
5. **Fragmentar antes de codificar.** El Tech Lead descompone el ticket en 2–6
   subtareas verificables y ordenadas por dependencia. **Las subtareas de testing
   son parte del plan, no opcionales.**
6. **Definición de Hecho dura.** "Hecho" en Jira sólo si: validación determinista
   PASÓ (incluye tests) **y** el revisor semántico aprobó. Si no → "En revisión" (humano).

---

## 3. Roles / nodos del pipeline

```
context_ingestion → git_setup → planner → codebase_reader → dynamic_developer
        → regression_guard → validation_gate → quality_reviewer
        ─(loop ≤6)→ dynamic_developer
        quality_reviewer ─(val ✓ y aprobado)→ git_finalize → jira_updater
```

| Nodo / rol | Responsabilidad |
|---|---|
| **context_ingestion** | Lee el ticket + **detecta el stack** (rails/flutter/node) por archivos marcador. |
| **git_setup** (GitOps) | Crea la rama `fleet/<ticket>-<slug>` desde `develop`/default. Árbol limpio. |
| **planner** (Tech Lead) | Descompone en subtareas atómicas con **paso de testing obligatorio**. |
| **codebase_reader** | Lee archivos existentes antes de modificar. Evita regresiones por sobrescritura. |
| **dynamic_developer** | Escribe código **con guardrails TDD** + playbook por rol + grounding. |
| **regression_guard** | Detecta modelos/campos/exports eliminados antes de correr los tests. |
| **validation_gate** (Validator) | **Determinista**: sintaxis + **Vitest/RSpec** + `bin/fleet-validate`. Devuelve errores reales. |
| **quality_reviewer** | Si la validación falló → realimenta errores. Si pasó → LLM juzga semánticamente. |
| **git_finalize** (GitOps) | Commit en la rama + push + **PR**. WIP si no validó. Nunca mergea. |
| **jira_updater** | Comenta validación+revisor+PR. Transiciona solo si validó+aprobó. |

### Especialistas (labels `agent:<rol>` en Jira → `ROLE_PLAYBOOKS`)

| Agente | Rol |
|---|---|
| `agent:Rails` / `agent:Backend` | Modelo+migración+spec coherentes; enum⇒columna/attribute; TenantRecord. |
| `agent:Schema` | Guardián del esquema: reconcilia spec↔factory↔tabla. |
| `agent:Flutter` / `agent:Mobile` | Respeta contrato snake_case de la API; build verde. |
| `agent:QA` | **NUEVO:** Experto en calidad. Solo escribe tests (unit, integration, regression, E2E). Nunca modifica implementación. |
| Sin label → `Full-Stack` | Triangula antes de codificar. Incluye tests en su propio trabajo. |

---

## 4. TDD — Metodología obligatoria (v3)

### ¿Qué debe escribir el developer?

Para cada cambio de código, el developer (o el agente QA) **debe generar**:

| Tipo de test | Cuándo | Dónde | Framework |
|---|---|---|---|
| **Unit** | Toda función pura nueva | `src/lib/*.test.ts`, `mobile/lib/*.test.ts` | Vitest |
| **Integration** | Server Actions y API routes nuevas | `src/server/**/*.test.ts`, `src/lib/*.test.ts` | Vitest |
| **Regression** | Al modificar código existente | Junto al archivo modificado | Vitest |
| **E2E** | Flujos de usuario con UI + DB | `tests/e2e/*.spec.ts` | Playwright |

### Flujo TDD en el pipeline

```
Planner → incluye subtarea "N+1: Tests unitarios para <módulo>"
         ↓
Developer → escribe test ANTES del código (Red)
         ↓
Developer → escribe implementación mínima (Green)
         ↓
validation_gate → ejecuta npx vitest run
         ↓ si tests fallan
Developer (ciclo) → refactoriza hasta Green
         ↓ si tests pasan
quality_reviewer → aprueba semánticamente
```

### Guardrails TDD inyectados en el prompt del developer

```
[TDD — metodología obligatoria para todo cambio de código]
- Escribe los tests ANTES o JUNTO con el código (Red → Green → Refactor).
- Todo función pura nueva en src/lib/ o mobile/lib/ DEBE tener su *.test.ts.
- Framework: vitest. Import: { describe, it, expect } from "vitest".
- Un cambio NO está terminado hasta que npx vitest run pasa.
- No uses placeholders en tests ('// TODO: test this'). Los tests deben ser reales.
```

---

## 5. El agente QA (v3)

El agente `QA` es un especialista que puede incluirse en cualquier ticket añadiendo
el label `agent:QA` en Jira o pasando `agents=["QA"]` via API.

**Responsabilidad exclusiva:**
- Revisa el código generado por otros agentes
- Escribe los tests que faltan (sin modificar el código de implementación)
- Verifica cobertura de: unit, integration, regression y E2E
- Asegura que `npx vitest run` pasa con los nuevos tests

**Cuándo usarlo:**
- Al final de un ticket complejo (junto con Full-Stack o Node)
- Como agente único para tareas de "agregar tests a código existente"
- Como revisión de calidad de tests generados por otros agentes

**Playbook:**
```
Experto en calidad. Tu tarea es SOLO escribir tests, nunca el código de implementación.
Cubre: unit tests en src/lib/*.test.ts y mobile/lib/*.test.ts (Vitest),
integration tests para Server Actions y API routes, regression tests para cambios
en código existente, E2E en tests/e2e/*.spec.ts (Playwright).
Todos los tests deben ejecutar y pasar con `npx vitest run`.
```

---

## 6. El gate determinista (la pieza central)

`validation_gate_node` corre, en orden y best-effort según el toolchain disponible:

1. **Sintaxis Ruby** (`ruby -c`) — rails only. Atrapa corrupción de comillas/escapes.
2. **TypeScript** (`tsc --noEmit --skipLibCheck`) — node only. Atrapa errores de tipos.
3. **Vitest** (`npx vitest run`) — node only. **NUEVO en v3.** Corre todos los tests
   unitarios e integración. El gate FALLA si:
   - Los tests fallan, o
   - No hay archivos `*.test.ts` entre los generados (obliga a escribir tests)
4. **Comando del proyecto**: `FLEET_VALIDATE_CMD`, o `bin/fleet-validate`, o `make validate`.
   Aquí el proyecto corre **sus** tests con su toolchain.

> **bin/fleet-validate (v3).** Cada proyecto debe tener `bin/fleet-validate` — un script
> ejecutable que corre TypeScript + Vitest + cualquier otro check del proyecto.
> La Flota lo llama automáticamente si existe.
>
> Para el tennis-app: `app-tennis/bin/fleet-validate` corre `tsc --noEmit` + `vitest run`.
> Los tests E2E (Playwright) se omiten en la flota porque requieren un servidor Next.js.

---

## 7. Guardrails de ingeniería (inyectadas en cada prompt del dev)

Codificadas en `ENGINEERING_GUARDRAILS_*` (langgraph_fleet.py). Resumen:

### Todos los stacks:
- **Grounding**: no inventar modelos; alinear al código existente.
- **No placeholders**: `...`, `# TODO`, `# resto` = rechazo.
- **Archivos completos**: el último carácter de FILE_BEGIN/END es el cierre real.

### Rails:
- Triangulación spec↔factory↔tabla obligatoria.
- `enum` ⇒ columna backing o `attribute` explícito.
- TenantRecord para tablas en `db/tenant_migrate/`.
- Tests con `require "rails_helper"`.

### Node (Next.js + Prisma + TypeScript):
- TypeScript estricto; `tsc --noEmit` sin errores.
- Prisma migrations con cada cambio de schema.
- Server Components por defecto; `'use client'` solo cuando sea necesario.
- Mobile: React Native en `mobile/`, jamás en `src/`.
- **TDD**: tests unitarios (`*.test.ts`) para toda función pura nueva.
- Tipos de tests: unit → `src/lib/`, integration → `src/server/`, E2E → `tests/e2e/`.

---

## 8. GitFlow

- Base: `develop` si existe; si no, la rama por defecto del remoto.
- Rama de trabajo: `fleet/<TICKET>-<slug>` (una por ticket).
- Commit con cuerpo estructurado (ciclos, validación, revisor, ticket).
- `push -u origin` + `gh pr create --base <base>` → URL del PR en el comentario de Jira.
- **Jamás** merge automático a `main`. Revisión humana del PR.

---

## 9. Modo requerimiento libre — cualquier proyecto, sin Jira

La Flota resuelve **cualquier requerimiento de código en cualquier proyecto de Claude**,
no solo tarjetas de Jira. Jira es ahora **opcional**.

**Montaje:** la raíz de proyectos se monta en `/projects` (`PROJECTS_DIR`, default el
padre de `n8n/`). Cualquier proyecto es alcanzable como `/projects/<Nombre>`.

**Entradas equivalentes (mismo pipeline v3):**

| Vía | Cómo |
|---|---|
| **MCP** (Claude Code) | `resolver_requerimiento(requerimiento, proyecto, agentes?)` |
| **HTTP** | `POST /solve` `{ "requirement": "...", "workspace": "/projects/<Nombre>", "agents": ["Node", "QA"] }` |
| **CLI** | `python langgraph_fleet.py --requirement "..." --workspace /projects/<Nombre> --agents Node,QA` |
| Jira (compat) | `resolver_ticket_jira(ticket_id)` · `POST /run` · `--ticket` |

---

## 10. bin/fleet-validate — integración por proyecto

Cada proyecto debe exponer `bin/fleet-validate` (ejecutable) que la Flota llama
automáticamente. Si no existe, la flota avisa y no puede ejecutar tests reales.

### Template genérico (Node/Next.js):

```bash
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
FAILED=0

echo "→ TypeScript..."
npx tsc --noEmit --skipLibCheck || FAILED=1

echo "→ Vitest (unit + integration)..."
npx vitest run --reporter=verbose || FAILED=1

echo "ℹ  Tests E2E omitidos (requieren servidor). Ejecutar manualmente: npm run test:e2e"

[ "$FAILED" -eq 0 ] && echo "✓ VALIDACIÓN PASÓ" && exit 0
echo "✗ VALIDACIÓN FALLÓ" && exit 1
```

### Proyectos soportados:

| Proyecto | Script | Tests cubiertos |
|---|---|---|
| `app-tennis` (Next.js) | `bin/fleet-validate` | tsc + vitest (175 tests) |
| `obra_viva` (Rails) | `bin/fleet-validate` | ruby -c + rspec |

### Configurar en `.env` de la flota:

```bash
FLEET_VALIDATE_CMD=bin/fleet-validate   # o dejar vacío si el script existe en el proyecto
FLEET_VALIDATE_TIMEOUT=300              # segundos (default 600)
FLEET_VITEST_TIMEOUT=180               # timeout específico para vitest (default 180)
```

---

## 11. Cómo aplicar / desplegar (v3)

```bash
# Reconstruir la imagen y levantar
docker compose --project-directory ~/Documents/Claude/Projects/n8n \
  -f ~/Documents/Claude/Projects/n8n/docker-compose.yml up -d --build

# Verificar que bin/fleet-validate existe en el proyecto
ls /projects/sdelvillar/tennis-app/app-tennis/bin/fleet-validate

# Lanzar un requerimiento con agente QA incluido
curl -s -X POST http://localhost:8000/solve \
  -H "Content-Type: application/json" \
  -H "X-Wait: true" \
  -d '{
    "requirement": "Agrega campo de teléfono al modelo Coach con validación E.164",
    "workspace": "/projects/sdelvillar/tennis-app/app-tennis",
    "agents": ["Node", "QA"]
  }'
```

---

## 12. Roadmap (siguiente iteración)

- **Sub-grafo por subtarea**: un ciclo dev→validate por subtarea en vez de un dump por ticket.
- **Playwright en CI**: correr tests E2E con servidor efímero en el contenedor de la flota.
- **Métrica "first-pass test rate"**: cuántos tickets pasan el gate de vitest en el primer ciclo.
- **Persistencia de cadena de fallos** por ticket para evitar errores repetidos entre ciclos.
- **Test coverage report**: integrar istanbul/nyc y fallar si coverage baja del umbral.
