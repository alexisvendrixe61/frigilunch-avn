#!/bin/bash
# Claude Code Status Line - Shows connected Azure environments
# Resolve script directory so paths work regardless of CWD
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
input=$(cat)

# Colors
GREEN='\033[32m'
YELLOW='\033[33m'
RED='\033[31m'
DIM='\033[2m'
BOLD='\033[1m'
RESET='\033[0m'

# Brand colors
FABRIC_GREEN='\033[38;2;0;120;72m'       # Microsoft Fabric green (#007848)
DATABRICKS_RED='\033[38;2;255;59;36m'    # Databricks red (#FF3B24)
FOUNDRY_PURPLE='\033[38;2;105;42;115m'   # Azure AI Foundry aubergine (#692A73)
LOGANALYTICS_BLUE='\033[38;2;0;120;212m' # Azure Log Analytics blue (#0078D4)

# Parse context % from stdin
PCT=$(echo "$input" | grep -o '"used_percentage":[0-9]*' | head -1 | sed 's/"used_percentage"://')
[ -z "$PCT" ] && PCT="0"

# Context color based on usage
if [[ "$PCT" -lt 50 ]]; then
  CTX_COLOR="$GREEN"
elif [[ "$PCT" -lt 80 ]]; then
  CTX_COLOR="$YELLOW"
else
  CTX_COLOR="$RED"
fi

# Read azure-config.json for per-service tenant + user info
CONFIG_FILE="$SCRIPT_DIR/azure-config.json"

# Helper: look up tenant name by ID from config
lookup_tenant() {
  local tenant_id="$1"
  local name
  name=$(grep -o "\"$tenant_id\"[[:space:]]*:[[:space:]]*\"[^\"]*\"" "$CONFIG_FILE" 2>/dev/null | head -1 | sed 's/.*:[[:space:]]*"//;s/"//')
  [ -n "$name" ] && echo "$name" || echo "$tenant_id"
}

# Helper: get service field from config
get_service_field() {
  local service="$1"
  local field="$2"
  local value
  value=$(sed -n "/$service/,/}/p" "$CONFIG_FILE" 2>/dev/null | grep -o "\"$field\"[[:space:]]*:[[:space:]]*\"[^\"]*\"" | head -1 | sed 's/.*:[[:space:]]*"//;s/"//')
  echo "$value"
}

# Read all 4 services
FOUNDRY_TENANT=""
FOUNDRY_USER=""
DATABRICKS_TENANT=""
DATABRICKS_USER=""
FABRIC_TENANT=""
FABRIC_USER=""
LOGANALYTICS_TENANT=""
LOGANALYTICS_USER=""

if [ -f "$CONFIG_FILE" ]; then
  FOUNDRY_TID=$(get_service_field "foundry" "tenant")
  FOUNDRY_USER=$(get_service_field "foundry" "user")
  DATABRICKS_TID=$(get_service_field "databricks" "tenant")
  DATABRICKS_USER=$(get_service_field "databricks" "user")
  FABRIC_TID=$(get_service_field "fabric" "tenant")
  FABRIC_USER=$(get_service_field "fabric" "user")
  LOGANALYTICS_TID=$(get_service_field "loganalytics" "tenant")
  LOGANALYTICS_USER=$(get_service_field "loganalytics" "user")

  [ -n "$FOUNDRY_TID" ] && FOUNDRY_TENANT=$(lookup_tenant "$FOUNDRY_TID")
  [ -n "$DATABRICKS_TID" ] && DATABRICKS_TENANT=$(lookup_tenant "$DATABRICKS_TID")
  [ -n "$FABRIC_TID" ] && FABRIC_TENANT=$(lookup_tenant "$FABRIC_TID")
  [ -n "$LOGANALYTICS_TID" ] && LOGANALYTICS_TENANT=$(lookup_tenant "$LOGANALYTICS_TID")
fi

# Build output
OUTPUT="${CTX_COLOR}${PCT}%${RESET} ctx"

# AI Foundry (aubergine purple)
if [ -n "$FOUNDRY_TENANT" ]; then
  OUTPUT="${OUTPUT} ${DIM}|${RESET} ${FOUNDRY_PURPLE}${BOLD}AI Foundry${RESET}${DIM}:${RESET} ${FOUNDRY_PURPLE}${FOUNDRY_TENANT}${RESET}"
  [ -n "$FOUNDRY_USER" ] && OUTPUT="${OUTPUT} ${DIM}(${FOUNDRY_USER})${RESET}"
fi

# Fabric (Microsoft Fabric green)
if [ -n "$FABRIC_TENANT" ]; then
  OUTPUT="${OUTPUT} ${DIM}|${RESET} ${FABRIC_GREEN}${BOLD}Fabric${RESET}${DIM}:${RESET} ${FABRIC_GREEN}${FABRIC_TENANT}${RESET}"
  [ -n "$FABRIC_USER" ] && OUTPUT="${OUTPUT} ${DIM}(${FABRIC_USER})${RESET}"
fi

# Databricks (Databricks red)
if [ -n "$DATABRICKS_TENANT" ]; then
  OUTPUT="${OUTPUT} ${DIM}|${RESET} ${DATABRICKS_RED}${BOLD}Databricks${RESET}${DIM}:${RESET} ${DATABRICKS_RED}${DATABRICKS_TENANT}${RESET}"
  [ -n "$DATABRICKS_USER" ] && OUTPUT="${OUTPUT} ${DIM}(${DATABRICKS_USER})${RESET}"
fi

# Log Analytics (Azure blue)
if [ -n "$LOGANALYTICS_TENANT" ]; then
  OUTPUT="${OUTPUT} ${DIM}|${RESET} ${LOGANALYTICS_BLUE}${BOLD}Logs${RESET}${DIM}:${RESET} ${LOGANALYTICS_BLUE}${LOGANALYTICS_TENANT}${RESET}"
  [ -n "$LOGANALYTICS_USER" ] && OUTPUT="${OUTPUT} ${DIM}(${LOGANALYTICS_USER})${RESET}"
fi

printf '%b\n' "$OUTPUT"

# Analytics notice (grey italic)
ITALIC='\033[3m'
GREY='\033[38;5;243m'
printf '%b\n' "${GREY}${ITALIC}Tool and skill invocations are logged to Azure Log Analytics for usage insights${RESET}"
