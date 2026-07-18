"""
Servidor MCP liviano que expone la Fleet API de LangGraph como herramienta
para Claude Code y cualquier cliente MCP compatible.
"""
import os
import httpx
from mcp.server.fastmcp import FastMCP

FLEET_API_URL = os.getenv("FLEET_API_URL", "http://localhost:8000")

mcp = FastMCP("n8n-agile-fleet")


def _format_result(result: dict) -> str:
    estado = "✅ Aprobado+validado" if result.get("approved") else "⚠️ Requiere revisión (no aprobó o no validó)"
    return (
        f"Tarea: {result.get('ticket_id')}\n"
        f"Estado: {estado}\n"
        f"Ciclos de revisión: {result.get('iterations')}\n\n"
        f"Resumen:\n{result.get('summary')}"
    )


@mcp.tool()
def resolver_ticket_jira(ticket_id: str) -> str:
    """
    Inicia la flota multi-agente LangGraph para resolver un ticket de Jira.
    Ejecuta el ciclo desarrollo → validación determinista → revisión y deja el
    trabajo en una rama + PR (actualiza el ticket de Jira automáticamente).

    Args:
        ticket_id: ID del ticket de Jira a resolver (ej: PROJ-404)
    """
    with httpx.Client(timeout=900) as client:
        response = client.post(
            f"{FLEET_API_URL}/run",
            json={"ticket_id": ticket_id, "workspace": "/workspace"},
            headers={"X-Wait": "true"},
        )
        response.raise_for_status()
        result = response.json()
    return _format_result(result)


@mcp.tool()
def resolver_requerimiento(requerimiento: str, proyecto: str, agentes: str = "") -> str:
    """
    Resuelve un requerimiento de código LIBRE (sin Jira) en CUALQUIER proyecto.
    Ejecuta el pipeline completo: planifica → implementa → valida (sintaxis/tests)
    → deja el trabajo en una rama `fleet/TASK-...` + Pull Request. Nunca toca main.

    Args:
        requerimiento: Qué construir o arreglar, en texto libre (criterios de aceptación).
        proyecto: Nombre de la carpeta del proyecto bajo la raíz de Claude
                  (ej: "obra_viva" → /projects/obra_viva), o una ruta absoluta del contenedor.
        agentes: (opcional) roles separados por coma, ej. "Rails,Schema". Default: Full-Stack.
    """
    workspace = proyecto if proyecto.startswith("/") else f"/projects/{proyecto}"
    agents = [a.strip() for a in agentes.split(",") if a.strip()] or None
    with httpx.Client(timeout=900) as client:
        response = client.post(
            f"{FLEET_API_URL}/solve",
            json={"requirement": requerimiento, "workspace": workspace, "agents": agents},
            headers={"X-Wait": "true"},
        )
        response.raise_for_status()
        result = response.json()
    return _format_result(result)


if __name__ == "__main__":
    mcp.run(transport="stdio")
