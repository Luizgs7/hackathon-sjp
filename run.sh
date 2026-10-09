#!/usr/bin/env bash
# Sobe a plataforma (http://localhost:8000) e o legado simulado (http://localhost:8001).
# Uso: ./run.sh            → inicia
#      ./run.sh --reset    → apaga bancos/uploads e recomeça com os dados de demonstração
set -euo pipefail
cd "$(dirname "$0")"

if [[ "${1:-}" == "--reset" ]]; then
  rm -f plataforma.db* legado.db* && rm -rf uploads && echo "Bancos de demonstração apagados."
fi

if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi
.venv/bin/pip install -q -r requirements.txt
[[ -f .env ]] || { cp .env.example .env; echo "Criado .env a partir de .env.example — preencha LLM_API_KEY para usar a IA."; }

trap 'kill 0' EXIT INT TERM
.venv/bin/uvicorn legado:app --host 0.0.0.0 --port 8001 --log-level warning &
.venv/bin/uvicorn app:app --host 0.0.0.0 --port 8000 &
sleep 2
echo
echo "  Plataforma Missões SJP ...... http://localhost:8000"
echo "  SisChamados Legado (simulado) http://localhost:8001"
echo "  Documentação da API ......... http://localhost:8000/docs"
echo "  Ctrl+C para encerrar."
wait
