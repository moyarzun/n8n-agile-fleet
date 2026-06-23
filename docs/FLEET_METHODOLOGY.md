# Metodología de la Flota de Agentes — v2 (endurecida)

> Rediseño tras una sesión real donde la Flota generó código que **no parseaba, no
> booteaba y nunca se ejecutó**, obligando a un humano a reescribirlo. Este documento
> explica qué falló, los principios nuevos, y cómo el pipeline los implementa.

## 1. Diagnóstico: por qué la v1 producía trabajo inservible

| Síntoma observado en producción | Causa raíz en la v1 |
|---|---|
| Código que no parseaba (comillas escapadas, dobles backslashes) | El "reviewer" era un **LLM leyendo archivos truncados**; nunca se corría `ruby -c`. |
| App no booteaba (initializers de gemas inexistentes, `enum` sin columna → "Undeclared attribute type") | **No había boot check** ni `zeitwerk:check`. |
| Modelo, tabla y spec con **3 diseños distintos** (inglés/español, columnas inexistentes) | El dev generaba modelos **sin anclarse** al spec/factory/esquema reales. |
| Commits directos en `main`, trabajo no revertible, ramas alucinadas | **No había git**: el dev escribía archivos sueltos al workspace. |
| "Aprobado" pero los tests fallaban | El quality gate era **opinión de un LLM**, sin ejecución determinista. |
| Respuestas truncadas / placeholders `# TODO` | Un **único dump monolítico** de "todos los archivos" en una sola llamada. |

**Conclusión:** ningún ajuste de prompt arregla esto. Falta *ejecutar el código* y
*aislar el trabajo en git*. Eso es lo que añade la v2.

## 2. Principios

1. **El código se ejecuta, no se "lee".** Ningún cambio avanza sin pasar un gate
   determinista (sintaxis + boot + tests). Los errores reales —no opiniones— se
   realimentan al desarrollador.
2. **La fuente de verdad ya existe.** Para Rails: el SPEC manda; modelo, migración y
   factory se alinean a él (triangulación spec↔factory↔esquema). Prohibido inventar.
3. **Trabajo aislado y revertible.** Cada ticket vive en su rama `fleet/<ticket>-<slug>`
   con commit + PR. **Nunca** se toca `main`.
4. **Fragmentar antes de codificar.** El Tech Lead descompone el ticket en 2–6
   subtareas verificables y ordenadas por dependencia.
5. **Definición de Hecho dura.** "Hecho" en Jira sólo si validación determinista
   PASÓ **y** el revisor semántico aprobó. Si no → "En revisión" (humano).

## 3. Roles / nodos del pipeline

```
context_ingestion → git_setup → planner → dynamic_developer
        → validation_gate → quality_reviewer ─(loop ≤6)→ dynamic_developer
        quality_reviewer ─(val ✓ y aprobado)→ git_finalize → jira_updater
```

| Nodo / rol | Responsabilidad nueva |
|---|---|
| **context_ingestion** | Lee el ticket + **detecta el stack** (rails/flutter/node) por archivos marcador. |
| **git_setup** (GitOps) | Crea la rama `fleet/<ticket>-<slug>` desde `develop`/default. Árbol limpio. |
| **planner** (Tech Lead) | Descompone en subtareas atómicas **ancladas al código real** (grounding). |
| **dynamic_developer** | Escribe código con **guardrails de ingeniería** + **playbook por rol** + grounding (schema+spec+factory) + la checklist de subtareas. |
| **validation_gate** (Validator) | **Determinista**: `ruby -c` de cada `.rb` + `bin/fleet-validate` del proyecto (boot + rspec). Devuelve errores reales. |
| **quality_reviewer** | Si la validación falló → realimenta los errores **sin gastar LLM**. Si pasó → el LLM juzga la aceptación **semántica**. |
| **git_finalize** (GitOps) | Commit en la rama + push + **PR** (gh CLI). WIP si no validó. Nunca mergea. |
| **jira_updater** | Comenta validación+revisor+PR. Transiciona a "Hecho" sólo si validó+aprobó; si no, "En revisión". |

### Especialistas (labels `agent:<rol>` en Jira → `ROLE_PLAYBOOKS`)
- `agent:Rails` / `agent:Backend` — modelo+migración+spec coherentes; enum⇒columna/attribute; TenantRecord para tablas de tenant.
- `agent:Schema` — guardián del esquema: reconcilia spec↔factory↔tabla, migraciones idempotentes.
- `agent:Flutter` / `agent:Mobile` — respeta el contrato snake_case de la API; build verde.
- Sin label → `Full-Stack` (triangula antes de codificar).

## 4. El gate determinista (la pieza central)

`validation_gate_node` corre, en orden y best-effort según el toolchain disponible:

1. **Sintaxis Ruby** (`ruby -c`) sobre cada `.rb` tocado — siempre que haya `ruby`
   (ya incluido en la imagen). Atrapa la corrupción de comillas/escapes y los placeholders.
2. **Comando del proyecto**: `FLEET_VALIDATE_CMD`, o `bin/fleet-validate`, o `make validate`.
   Ahí el proyecto corre **sus** tests con su toolchain. En `obra_viva`,
   `bin/fleet-validate` hace: barrido `ruby -c` → `rails zeitwerk:check` (boot) →
   `rspec` de los specs tocados/inferidos.

> **Toolchain.** El contenedor de la Flota tiene `ruby` y `git` (Dockerfile), suficiente
> para sintaxis + GitFlow. Para correr la suite completa (rspec + Postgres) define
> `FLEET_VALIDATE_CMD` apuntando a un entorno Rails (host con la toolchain, o un
> servicio *validator* sidecar). Sin eso, el gate corre sólo sintaxis y lo **avisa**
> (no aprueba a ciegas).

## 5. Guardrails de ingeniería (inyectadas en cada prompt del dev)

Codificadas en `ENGINEERING_GUARDRAILS` (langgraph_fleet.py). Resumen:
- **Grounding**: no inventar modelos; alinear a spec+factory+esquema; no traducir nombres de columna.
- **Enums**: `enum` requiere columna backing o `attribute` explícito (o crashea con eager_load).
- **Multitenancy**: tabla en `db/tenant_migrate/` ⇒ modelo `TenantRecord`; no asociar control↔tenant.
- **Migraciones**: toda columna usada debe existir; el tipo del enum coincide con el de la columna.
- **Tests**: specs con `require "rails_helper"`; "hecho" = spec verde + `ruby -c`.
- **Boot**: no crear initializers con APIs/sintaxis inexistentes.

## 6. GitFlow

- Base: `develop` si existe; si no, la rama por defecto del remoto.
- Rama de trabajo: `fleet/<TICKET>-<slug>` (una por ticket).
- Commit con cuerpo estructurado (ciclos, validación, revisor, ticket).
- `push -u origin` + `gh pr create --base <base>` → URL del PR en el comentario de Jira.
- **Jamás** merge automático a `main`. Revisión humana del PR.

## 7. Cómo aplicar / desplegar

```bash
# Reconstruir la imagen (añade git + ruby) y levantar
docker compose --project-directory ~/Documents/Claude/Projects/n8n \
  -f ~/Documents/Claude/Projects/n8n/docker-compose.yml up -d --build

# (Opcional) validación completa con la suite real del proyecto:
#   en n8n/.env →  FLEET_VALIDATE_CMD=bin/fleet-validate
# y asegurar que el workspace corre en un entorno con Ruby/Bundler/Postgres.

# Lanzar un ticket (igual que antes)
curl -s -X POST http://localhost:8000/run \
  -H "Content-Type: application/json" \
  -d '{"ticket_id":"SCRUM-XX","workspace":"/workspace"}' --max-time 900 &
```

## 8. Roadmap (siguiente iteración)

- **Validator sidecar**: contenedor Rails con la toolchain + Postgres para correr la
  suite completa sin acoplar la imagen de la Flota.
- **Persistencia de cadena de fallos** por ticket para que el dev no repita errores entre ciclos.
- **Sub-grafo por subtarea** (un ciclo dev→validate por subtarea) en vez de un dump por ticket.
- **Métrica de "first-pass validation rate"** para medir si las guardrails reducen reciclos.
