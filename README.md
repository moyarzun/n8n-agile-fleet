# n8n-agile-fleet

Fleet multi-agente LangGraph que resuelve tickets Jira automáticamente. Ciclo dev → review → aprobación sin intervención humana.

**Stack:** MiniMax M2.7 (primario) + OpenRouter free models (fallback). Sin costos de API de Claude/OpenAI.

---

## Requisitos

- Docker + Docker Compose
- Cuenta en [MiniMax](https://api.minimax.io) (dev primario)
- Cuenta en [OpenRouter](https://openrouter.ai) (fallback gratuito)
- Cuenta en Jira Cloud

---

## Setup en 3 pasos

```bash
# 1. Clonar
git clone https://github.com/moyarzun/n8n-agile-fleet.git
cd n8n-agile-fleet

# 2. Configurar variables de entorno
cp .env.example .env
# Editar .env con tus API keys

# 3. Levantar
make start
```

El fleet queda disponible en `http://localhost:8000` y el dashboard en tiempo real en `http://localhost:8000/`.

---

## Variables de entorno

| Variable | Descripción |
|---|---|
| `MINIMAX_API_KEY` | API key de MiniMax (modelo primario del developer) |
| `OPENROUTER_API_KEY` | API key de OpenRouter (fallback gratuito) |
| `JIRA_URL` | URL de tu Jira Cloud, ej. `https://company.atlassian.net` |
| `JIRA_USER` | Email de tu cuenta Jira |
| `JIRA_API_TOKEN` | Token de API de Jira ([generarlo aquí](https://id.atlassian.com/manage-profile/security/api-tokens)) |
| `WORKSPACE_DIR` | Ruta absoluta del proyecto de software que la flota modificará |
| `FLEET_ASYNC_WORKERS` | Workers para jobs async (default: `8`) |
| `FLEET_WAIT_WORKERS` | Workers para requests síncronos (default: `4`) |

---

## Uso básico

```bash
# Lanzar un ticket (async — retorna job_id inmediatamente)
curl -X POST http://localhost:8000/run \
  -H "Content-Type: application/json" \
  -d '{"ticket_id": "PROJ-123", "workspace": "/workspace"}'

# Ver estado del job
curl http://localhost:8000/status/<job_id>

# Dashboard en tiempo real
open http://localhost:8000/
```

---

## Instalación como herramienta en agentes IA

### Claude Code

```bash
make install-claude
```

O manualmente — agregar a `~/.claude/settings.json`:

```json
{
  "mcpServers": {
    "n8n-agile-fleet": {
      "command": "python3",
      "args": ["/ruta/a/n8n-agile-fleet/agile_scripts/mcp_fleet_server.py"],
      "env": {
        "FLEET_API_URL": "http://localhost:8000"
      }
    }
  }
}
```

### Gemini CLI

```bash
make install-gemini
```

O manualmente — agregar a `~/.gemini/settings.json`:

```json
{
  "mcpServers": {
    "n8n-agile-fleet": {
      "command": "python3",
      "args": ["/ruta/a/n8n-agile-fleet/agile_scripts/mcp_fleet_server.py"],
      "env": {
        "FLEET_API_URL": "http://localhost:8000"
      }
    }
  }
}
```

### ChatGPT (Custom Actions)

1. En tu Custom GPT → *Actions* → *Create new action*
2. Importar el schema desde `openapi.yaml` o desde `http://localhost:8000/openapi.json`
3. Configurar la URL base: `http://localhost:8000`

### Cualquier agente con soporte REST

Usa el endpoint directamente. El schema OpenAPI está en:
- `openapi.yaml` en este repo
- `http://localhost:8000/openapi.json` (cuando el fleet está corriendo)

### Cualquier agente con soporte MCP

```bash
# Correr el servidor MCP en modo stdio
python3 agile_scripts/mcp_fleet_server.py

# O en modo HTTP (SSE)
FLEET_API_URL=http://localhost:8000 python3 agile_scripts/mcp_fleet_server.py
```

---

## Comandos

```bash
make setup      # Copiar .env.example a .env
make start      # Levantar los contenedores
make stop       # Detener los contenedores
make restart    # Reiniciar fleet-api
make logs       # Ver logs en tiempo real
make dashboard  # Abrir el dashboard en el browser
make status     # Estado de los contenedores
make install    # Detectar agente instalado y registrar MCP automáticamente
```

---

## Dashboard

El dashboard web está disponible en `http://localhost:8000/` (o la IP Tailscale del host). No requiere login.

### Pestaña: Ejecuciones

Vista principal del fleet en tiempo real.

```
┌─────────────────────────────────────────────────────────┐
│  ● Fleet Dashboard          Conectado   [📋 Ver logs]   │
├────────────┬────────────────────────────────────────────┤
│ Ejecuciones│ Métricas de tokens                         │
├────────────┴────────────────────────────────────────────┤
│ Ordenar: [Nombre ↑] [Estado] [Edad ↓] [Ciclo] [Archivos]│
├─────────────────────────────────────────────────────────┤
│ ┌───────────────────────────────────────────────────┐   │
│ │ SCRUM-45                          [⏹ Detener] 🔄  │   │
│ │ ● Desarrollando · Ciclo 2 · 8 arch. · 4m 12s      │   │
│ │ [18:32:01] Escribiendo implementación...           │   │
│ │ ▼ ver logs completos                               │   │
│ └───────────────────────────────────────────────────┘   │
│  ...                                                     │
├─────────────────────────────────────────────────────────┤
│ Por página: [10▾]              ‹  1 / 4  ›              │
└─────────────────────────────────────────────────────────┘
```

**Tarjetas de ejecución** — cada job muestra:

| Campo | Descripción |
|---|---|
| Ticket ID | Enlace visual al ticket Jira |
| Estado | `running` · `queued` · `approved` · `rejected` · `error` · `stopped` · `interrupted` |
| Fase actual | Contexto · Desarrollando · Revisando · Actualizando Jira |
| Ciclo | Iteración de dev→review actual |
| Archivos | Cantidad de archivos modificados en el workspace |
| Tiempo transcurrido | Actualizado en tiempo real cada segundo |
| Preview de logs | Últimas 3 líneas del agente |

**Controles:**

- **Ordenar** — barra con 5 criterios: Nombre (alfabético), Estado (running primero), Edad, Ciclo, Archivos. Clic al criterio activo alterna ↑ ascendente / ↓ descendente.
- **Paginación** — selector de 5 / 10 / 25 tarjetas por página.
- **⏹ Detener** — envía señal de parada graciosa al job (termina el step actual y actualiza Jira antes de salir).
- **▼ ver logs completos** — abre modal a pantalla completa con el log íntegro del agente. El scroll sigue automáticamente si estás al final.

**Vista móvil** (< 600 px): las tarjetas colapsan a filas compactas (sin preview de log). Toca una fila para expandirla al modo tarjeta completo.

**Botón "📋 Ver logs"** (header) — abre un panel con todas las ejecuciones, incluyendo el badge de estado, fase, ciclo y los botones directos a Jira y al PR de GitHub si el agente lo creó.

**Indicador de conexión** — punto verde (pulsante) = SSE activo; punto rojo = reconectando. El dashboard se reconecta automáticamente sin perder el estado.

---

### Pestaña: Métricas de tokens

Consumo acumulado de tokens por modelo LLM, con histórico gráfico.

```
┌─────────────────────────────────────────────────────────┐
│ Uso de tokens por modelo    [24h][7d][30d][90d][1 año]  │
│                                          Actualizado... │
├─────────────────────────────────────────────────────────┤
│  [Gráfico barras apiladas — tokens por día/hora]        │
│   Eje X: fecha/hora según período                       │
│   Eje Y: tokens (k)   · Colores por modelo              │
├─────────────────────────────────────────────────────────┤
│ 1,234,567   234,567   1,000,000   42                    │
│ Tokens tot.  Entrada   Salida      Llamadas LLM         │
├────────────────────┬───────┬───────┬──────┬────┬───────┤
│ Modelo             │ Calls │ Entr. │ Sal. │Tot │ Dist. │
├────────────────────┼───────┼───────┼──────┼────┼───────┤
│ MiniMax-M2.7       │   31  │ 890k  │ 344k │...│ ████  │
│ qwen3-coder:free   │    8  │ 123k  │  45k │...│ ██    │
│ llama-3.3-70b:free │    3  │  44k  │  12k │...│ █     │
└────────────────────┴───────┴───────┴──────┴────┴───────┘
```

**Períodos disponibles:**

| Botón | Ventana | Agrupación |
|---|---|---|
| 24 h | Últimas 24 horas | Por hora |
| 7 d | Últimos 7 días | Por día |
| 30 d | Últimos 30 días | Por día |
| 90 d | Últimos 90 días | Por día |
| 1 año | Últimos 365 días | Por mes |

- Los datos se almacenan en la tabla `token_events` de `fleet.db` y persisten entre reinicios.
- El gráfico y la tabla se actualizan **en tiempo real** vía SSE mientras hay jobs corriendo: cada llamada al LLM emite un evento `token_update` al dashboard si la pestaña está activa.
- La **barra de distribución** (columna Dist.) muestra la proporción relativa de tokens de entrada (azul) y salida (verde) de cada modelo respecto al modelo con mayor consumo.

---

## API

Ver `openapi.yaml` para el schema completo. Endpoints principales:

| Método | Path | Descripción |
|---|---|---|
| `POST` | `/run` | Lanzar ticket (async) |
| `POST` | `/stop/{job_id}` | Detener job en curso |
| `GET` | `/status` | Todos los jobs activos |
| `GET` | `/status/{job_id}` | Job específico |
| `GET` | `/events` | SSE — stream en tiempo real |
| `GET` | `/metrics` | Totales de tokens por modelo (acumulado) |
| `GET` | `/metrics/history?period=week` | Serie temporal por período |
| `GET` | `/` | Dashboard web |
| `GET` | `/health` | Health check |

**Períodos válidos para `/metrics/history`:** `day` · `week` · `month` · `quarter` · `year`

---

## Arquitectura

```
POST /run
    └─ ThreadPoolExecutor
           └─ engine.stream() [LangGraph]
                  ├─ context_ingestion   — lee ticket Jira + workspace
                  ├─ dynamic_developer   — MiniMax M2.7 escribe código
                  ├─ quality_reviewer    — MiniMax M2.7 revisa
                  └─ jira_updater        — actualiza ticket en Jira
                         ↓
                    SSE broadcast → GET /events → dashboard
```

Modelos: MiniMax M2.7 → qwen/qwen3-coder:free → nvidia/nemotron → llama-3.3-70b (fallback en cascada, sin Claude/OpenAI).
