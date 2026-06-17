"""
Servidor MCP liviano que expone la Fleet API de LangGraph como herramienta
para Claude Code y cualquier cliente MCP compatible.
"""
import os
import httpx
from mcp.server.fastmcp import FastMCP

FLEET_API_URL = os.getenv("FLEET_API_URL", "http://localhost:8000")

mcp = FastMCP("n8n-agile-fleet")


@mcp.tool()
def resolver_ticket_jira(ticket_id: str) -> str:
    """
    Inicia la flota multi-agente LangGraph para resolver un ticket de Jira.
    Ejecuta el ciclo desarrollo → revisión → aprobación y actualiza el ticket automáticamente.

    Args:
        ticket_id: ID del ticket de Jira a resolver (ej: PROJ-404)
    """
    with httpx.Client(timeout=600) as client:
        response = client.post(
            f"{FLEET_API_URL}/run",
            json={"ticket_id": ticket_id, "workspace": "/workspace"},
        )
        response.raise_for_status()
        result = response.json()

    return (
        f"Ticket: {result['ticket_id']}\n"
        f"Estado: {'✅ Aprobado' if result['approved'] else '⚠️ Máximo de iteraciones alcanzado'}\n"
        f"Ciclos de revisión: {result['iterations']}\n\n"
        f"Resumen:\n{result['summary']}"
    )


if __name__ == "__main__":
    mcp.run(transport="stdio")
