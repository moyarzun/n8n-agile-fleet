# Fleet Dashboard — Spec de Diseño

**Fecha:** 2026-06-17
**Proyecto:** n8n-agile-fleet
**Alcance:** Agregar dashboard de monitoreo en tiempo real al fleet LangGraph

---

## Objetivo

Exponer el estado de los jobs del fleet (tickets Jira en proceso) en tiempo real vía una UI web accesible desde Docker, sin polling — usando Server-Sent Events (SSE).

---

## Arquitectura

```
POST /run  →  crea job_id  →  lanza worker en ThreadPoolExecutor
                                      ↓
                         engine.stream() emite eventos LangGraph
                                      ↓
                         _broadcast(event)  →  asyncio.Queue por cliente SSE
                                                      ↓
GET /events  ←────────────────────  EventSource push (text/event-stream)
     ↓
browser actualiza card del job en tiempo real
```

### Estado en memoria

```python
_jobs: Dict[str, JobState]          # job_id → snapshot completo del job
_subscribers: List[asyncio.Queue]   # una Queue por cliente SSE conectado
```

`JobState` (TypedDict o dataclass):

| Campo | Tipo | Descripción |
|---|---|---|
| `job_id` | str | UUID |
| `ticket_id` | str | ej. "SCRUM-29" |
| `status` | str | `queued` \| `running` \| `approved` \| `rejected` \| `error` |
| `phase` | str | Nodo LangGraph activo: `context_ingestion` \| `dynamic_developer` \| `quality_reviewer` \| `jira_updater` |
| `iteration` | int | Ciclo dev→review actual |
| `files_count` | int | Archivos escritos al disco acumulados |
| `logs` | List[str] | Últimas 100 líneas de eventos del stream |
| `started_at` | str | ISO 8601 |
| `finished_at` | str \| None | ISO 8601 o null |
| `summary` | str | Texto final del reviewer |

Los jobs se conservan en memoria por 1 hora tras finalizar, luego se eliminan automáticamente.

---

## Endpoints

| Método | Path | Descripción |
|---|---|---|
| `POST` | `/run` | Retorna `{job_id, ticket_id}` inmediatamente; lanza worker en background |
| `GET` | `/events` | SSE — stream permanente; push de eventos a todos los clientes conectados |
| `GET` | `/status` | Snapshot JSON de todos los jobs (usado en carga inicial del dashboard) |
| `GET` | `/status/{job_id}` | Job específico con logs completos |
| `GET` | `/` | HTML del dashboard (inline, sin archivos estáticos separados) |
| `GET` | `/health` | Sin cambios — `{"status": "ok"}` |

### Contrato `/run` (nuevo)

**Request:** igual que antes — `{ticket_id, workspace}`

**Response (200):**
```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "ticket_id": "SCRUM-29"
}
```

Los clientes existentes (MCP, curl) que esperaban `FleetResponse` deben ahora hacer polling a `/status/{job_id}` hasta `status != "running"`, o conectarse a `/events`. Para compatibilidad hacia atrás, `/run` acepta un header opcional `X-Wait: true` que mantiene el comportamiento bloqueante anterior.

### Formato evento SSE

```
event: job_started
data: {"job_id":"...","ticket_id":"SCRUM-29","started_at":"2026-06-17T15:00:00Z"}

event: job_update
data: {"job_id":"...","phase":"dynamic_developer","iteration":2,
       "files_count":16,"status":"running",
       "log":"[developer] Escribiendo graph.py...","elapsed_s":47}

event: job_finished
data: {"job_id":"...","status":"approved","iterations":3,
       "files_count":46,"summary":"Arquitectura sólida...","elapsed_s":312}
```

---

## Compatibilidad hacia atrás

- `mcp_fleet_server.py` llama a `/run` y espera `FleetResponse`. Se agrega soporte para `X-Wait: true` en `/run` que mantiene la respuesta bloqueante con el mismo schema `FleetResponse`.
- Sin cambios a `docker-compose.yml` ni `Dockerfile` — no hay dependencias nuevas.

---

## UI (HTML inline en `fleet_api.py`)

- Servida en `GET /` como string HTML embebido en el endpoint FastAPI.
- Sin frameworks externos — HTML + CSS + JavaScript vanilla.
- Al cargar: `fetch('/status')` para poblar jobs existentes.
- `new EventSource('/events')` → escucha `job_started`, `job_update`, `job_finished`.
- Un **card por job** con:
  - Badge de ticket (ej. `SCRUM-29`)
  - Pill de fase con color: `context_ingestion` (gris), `dynamic_developer` (azul), `quality_reviewer` (amarillo), `jira_updater` (verde)
  - Contador de iteración y archivos escritos
  - Tiempo transcurrido (reloj en vivo para jobs activos)
  - Badge de estado con color: `running` (azul), `approved` (verde), `rejected` (rojo), `error` (naranja)
  - Log expandible — últimas 10 líneas visibles, scroll completo al expandir
- Reconexión automática de `EventSource` si cae la conexión (comportamiento nativo del browser).
- Cleanup automático de cards de jobs finalizados hace más de 1 hora.

---

## Lo que NO cambia

- `langgraph_fleet.py` — sin modificaciones.
- `mcp_fleet_server.py` — solo se actualiza la URL del endpoint si es necesario.
- `docker-compose.yml` — sin cambios.
- `Dockerfile` — sin cambios.

---

## Archivos modificados

| Archivo | Cambio |
|---|---|
| `agile_scripts/fleet_api.py` | Reescritura completa para agregar SSE, JobState, dashboard HTML |
