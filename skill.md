---
name: n8n-agile-fleet
description: Resuelve tickets Jira automáticamente usando una flota multi-agente LangGraph. El agente developer escribe el código, el reviewer lo aprueba, y el ticket se actualiza en Jira — sin intervención humana. Usar cuando el usuario pida resolver, implementar o completar un ticket de Jira.
metadata:
  type: tool
  version: "1.0.0"
  author: moyarzun
  repository: https://github.com/moyarzun/n8n-agile-fleet
  requires:
    - fleet running at FLEET_API_URL (default: http://localhost:8000)
---

# n8n-agile-fleet

Resuelve tickets Jira automáticamente con una flota LangGraph multi-agente.

## Cuándo usar

- El usuario pide "resolver", "implementar" o "completar" un ticket Jira
- El usuario menciona un ticket ID (ej: PROJ-123, SCRUM-42)
- El usuario quiere automatizar el desarrollo de una historia de usuario

## Cómo usar

### Lanzar un ticket (async)

```bash
curl -X POST $FLEET_API_URL/run \
  -H "Content-Type: application/json" \
  -d '{"ticket_id": "PROJ-123", "workspace": "/workspace"}'
# → {"job_id": "uuid", "ticket_id": "PROJ-123"}
```

### Verificar resultado

```bash
curl $FLEET_API_URL/status/<job_id>
# → {"status": "approved"|"rejected"|"running"|"error", ...}
```

### Ver dashboard en tiempo real

```
http://localhost:8000/
```

### Esperar resultado completo (bloqueante)

```bash
curl -X POST $FLEET_API_URL/run \
  -H "Content-Type: application/json" \
  -H "X-Wait: true" \
  -d '{"ticket_id": "PROJ-123"}' \
  --max-time 900
# → {"ticket_id": "...", "approved": true, "iterations": 3, "summary": "..."}
```

## Flujo interno

```
context_ingestion → dynamic_developer → quality_reviewer → jira_updater
                           ↑                    |
                           └────── feedback ────┘  (hasta 6 ciclos)
```

1. **context_ingestion** — lee el ticket Jira + escanea el workspace
2. **dynamic_developer** — MiniMax M2.7 escribe código completo con FILE_BEGIN/END
3. **quality_reviewer** — MiniMax M2.7 revisa contra criterios de aceptación
4. **jira_updater** — comenta en el ticket y lo transiciona a Done

## Estados del job

| Status | Descripción |
|---|---|
| `queued` | En cola, esperando worker |
| `running` | En proceso — algún nodo LangGraph activo |
| `approved` | Reviewer aprobó — ticket actualizado en Jira |
| `rejected` | Agotó 6 ciclos sin aprobación |
| `error` | Error inesperado — ver campo `summary` |

## Prerrequisitos

- Fleet corriendo: `cd /ruta/a/n8n-agile-fleet && make start`
- Variables de entorno configuradas: `MINIMAX_API_KEY`, `OPENROUTER_API_KEY`, `JIRA_URL`, `JIRA_USER`, `JIRA_API_TOKEN`, `WORKSPACE_DIR`

## Setup inicial

```bash
git clone https://github.com/moyarzun/n8n-agile-fleet.git
cd n8n-agile-fleet
make setup   # crea .env desde .env.example
# editar .env con tus keys
make start
```
