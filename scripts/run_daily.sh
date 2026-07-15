#!/usr/bin/env bash
# Daily collect → Top-3 tech/tool digest → WeChat push
# Usage (Linux server cron @ 09:00):
#   0 9 * * * /opt/ai-knowledge-base/scripts/run_daily.sh >> /var/log/ai-kb.log 2>&1
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ -f "$ROOT/.env" ]]; then
  # shellcheck disable=SC1091
  set -a && source "$ROOT/.env" && set +a
fi

PYTHON="${ROOT}/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  PYTHON="$(command -v python3)"
fi

echo "==> [$(date -Iseconds)] pipeline collect+analyze (fresh 7d)"
"$PYTHON" pipeline/pipeline.py --sources github,rss --limit 20 --fresh-days 7 --verbose

echo "==> [$(date -Iseconds)] digest push top-3 (tech/tool, last 3 days)"
"$PYTHON" pipeline/digest_push.py --limit 3 --days 3 --verbose

echo "==> [$(date -Iseconds)] done"
