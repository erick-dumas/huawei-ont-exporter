#!/usr/bin/env bash
# Crea el venv (si no existe), instala dependencias y arranca el exporter.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install --upgrade pip >/dev/null
pip install -r requirements.txt

[ -f .env ] || { cp .env.example .env; echo "[!] Se creó .env; edítalo y vuelve a ejecutar."; exit 1; }

PORT=$(grep -E '^APP_PORT=' .env | cut -d= -f2 || true)
exec uvicorn main:app --host 0.0.0.0 --port "${PORT:-8000}"
