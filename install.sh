#!/usr/bin/env bash
# install.sh — Registra n8n-agile-fleet como herramienta MCP en el agente detectado.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$REPO_DIR/agile_scripts/mcp_fleet_server.py"
FLEET_URL="${FLEET_API_URL:-http://localhost:8000}"
AGENT="${1:-}"

# ── Helpers ───────────────────────────────────────────────────────────────────

info()    { echo "  $*"; }
success() { echo "✓ $*"; }
warn()    { echo "⚠ $*"; }
error()   { echo "✗ $*" >&2; exit 1; }

merge_mcp_entry() {
  local settings_file="$1"
  local dir; dir="$(dirname "$settings_file")"
  mkdir -p "$dir"

  if [ ! -f "$settings_file" ]; then
    echo '{}' > "$settings_file"
  fi

  # Usa Python para hacer merge seguro del JSON sin sobreescribir otras keys
  python3 - "$settings_file" "$SCRIPT" "$FLEET_URL" <<'PYEOF'
import json, sys
settings_file, script, fleet_url = sys.argv[1], sys.argv[2], sys.argv[3]

with open(settings_file) as f:
    config = json.load(f)

config.setdefault("mcpServers", {})
config["mcpServers"]["n8n-agile-fleet"] = {
    "command": "python3",
    "args": [script],
    "env": {"FLEET_API_URL": fleet_url}
}

with open(settings_file, "w") as f:
    json.dump(config, f, indent=2, ensure_ascii=False)
    f.write("\n")

print(settings_file)
PYEOF
}

# ── Detección automática ──────────────────────────────────────────────────────

detect_agent() {
  if [ -n "$AGENT" ]; then
    # --agent flag explícito
    case "$AGENT" in
      --agent) echo "${2:-}" ;;
      claude)  echo "claude" ;;
      gemini)  echo "gemini" ;;
      *)       echo "unknown" ;;
    esac
    return
  fi

  # Auto-detección por orden de preferencia
  if command -v claude &>/dev/null; then echo "claude"; return; fi
  if command -v gemini &>/dev/null; then echo "gemini"; return; fi
  echo "none"
}

# Parsear --agent <value>
DETECTED="none"
if [ "${1:-}" = "--agent" ] && [ -n "${2:-}" ]; then
  DETECTED="$2"
else
  DETECTED="$(detect_agent)"
fi

# ── Instalación ───────────────────────────────────────────────────────────────

echo ""
echo "n8n-agile-fleet — instalación de skill MCP"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
info "Script MCP:  $SCRIPT"
info "Fleet URL:   $FLEET_URL"
info "Agente:      $DETECTED"
echo ""

case "$DETECTED" in
  claude)
    SETTINGS="$HOME/.claude/settings.json"
    info "Registrando en Claude Code ($SETTINGS)..."
    WRITTEN="$(merge_mcp_entry "$SETTINGS")"
    success "Instalado en $WRITTEN"
    info "Reinicia Claude Code o ejecuta /reload-plugins para activar."
    ;;

  gemini)
    SETTINGS="$HOME/.gemini/settings.json"
    info "Registrando en Gemini CLI ($SETTINGS)..."
    WRITTEN="$(merge_mcp_entry "$SETTINGS")"
    success "Instalado en $WRITTEN"
    info "Reinicia Gemini CLI para activar."
    ;;

  none|unknown|*)
    warn "No se detectó Claude Code ni Gemini CLI instalados."
    echo ""
    echo "Instrucciones manuales:"
    echo ""
    echo "━━ Claude Code / Gemini CLI / Cursor (MCP) ━━"
    echo "Agrega esto a tu archivo de settings del agente:"
    echo ""
    cat <<JSONEOF
{
  "mcpServers": {
    "n8n-agile-fleet": {
      "command": "python3",
      "args": ["$SCRIPT"],
      "env": { "FLEET_API_URL": "$FLEET_URL" }
    }
  }
}
JSONEOF
    echo ""
    echo "━━ ChatGPT Custom Actions ━━"
    echo "Importa el schema desde: $FLEET_URL/openapi.json"
    echo "o usa el archivo openapi.yaml incluido en este repo."
    echo ""
    echo "━━ Cualquier agente REST ━━"
    echo "Usa directamente POST $FLEET_URL/run"
    echo "Schema completo en: openapi.yaml"
    ;;
esac

echo ""
