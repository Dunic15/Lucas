#!/usr/bin/env bash
# access.sh — il "tasto" per vedere tutti gli accessi del progetto Laura da terminale.
#
#   ./scripts/access.sh              # mappa di tutti gli accessi (NOMI, non valori) + stato servizi
#   ./scripts/access.sh --secrets    # mostra ANCHE i valori decrittati da SSM (attenzione: segreti a schermo)
#   ./scripts/access.sh get NAME     # stampa un singolo parametro SSM (es: get /laura/prod/RECALL_API_KEY)
#   ./scripts/access.sh env          # elenca le chiavi presenti nei file .env locali
#
# Nessun segreto è scritto in questo file: tutto è recuperato a runtime da AWS SSM / file locali.

set -euo pipefail

REGION="${AWS_REGION:-eu-central-1}"
SERVICE="${LAURA_SERVICE:-laura-backend}"
SSM_ROOT="/laura"

bold()  { printf '\033[1m%s\033[0m\n' "$*"; }
dim()   { printf '\033[2m%s\033[0m\n' "$*"; }
head_() { printf '\n\033[1;36m▓▓ %s\033[0m\n' "$*"; }
have()  { command -v "$1" >/dev/null 2>&1; }

# ── get: singolo parametro SSM ────────────────────────────────────────────────
if [[ "${1:-}" == "get" ]]; then
  [[ -n "${2:-}" ]] || { echo "uso: $0 get /laura/prod/NOME"; exit 1; }
  aws ssm get-parameter --name "$2" --with-decryption --region "$REGION" \
    --query 'Parameter.Value' --output text
  exit 0
fi

# ── env: chiavi nei file .env locali ─────────────────────────────────────────
if [[ "${1:-}" == "env" ]]; then
  for f in .env .env.filled .env.example; do
    [[ -f "$f" ]] || continue
    head_ "$f (solo NOMI chiave)"
    grep -oE '^[A-Z_][A-Z0-9_]*' "$f" | sort -u | sed 's/^/  /'
  done
  exit 0
fi

SHOW_SECRETS=0
[[ "${1:-}" == "--secrets" ]] && SHOW_SECRETS=1

bold "🔑  ACCESSI PROGETTO LAURA   (region: $REGION)"
dim  "$( [[ $SHOW_SECRETS == 1 ]] && echo 'MODE: valori decrittati visibili' || echo 'MODE: solo nomi — usa --secrets per i valori' )"

# ── 1. File di config locali ──────────────────────────────────────────────────
head_ "1. Config locali"
for f in .env .env.filled .mcp.json; do
  [[ -f "$f" ]] && printf '  ✓ %s\n' "$f" || printf '  ✗ %s (mancante)\n' "$f"
done
echo "  → chiavi .env:  ./scripts/access.sh env"

# ── 2. AWS: identità + SSM Parameter Store ───────────────────────────────────
head_ "2. AWS  (SSM Parameter Store)"
if ! have aws; then
  echo "  ✗ AWS CLI non installato"
else
  ident=$(aws sts get-caller-identity --query 'Arn' --output text 2>/dev/null || echo "NON AUTENTICATO")
  echo "  account: $ident"
  echo
  if [[ $SHOW_SECRETS == 1 ]]; then
    aws ssm get-parameters-by-path --path "$SSM_ROOT" --recursive --with-decryption \
      --region "$REGION" --query 'Parameters[].[Name,Value]' --output text 2>/dev/null \
      | sort | awk -F'\t' '{printf "  %-52s = %s\n", $1, $2}' \
      || echo "  (nessun parametro o accesso negato)"
  else
    aws ssm get-parameters-by-path --path "$SSM_ROOT" --recursive \
      --region "$REGION" --query 'Parameters[].Name' --output text 2>/dev/null \
      | tr '\t' '\n' | sort | sed 's/^/  /' \
      || echo "  (nessun parametro o accesso negato)"
  fi
fi

# ── 3. App Runner: URL + stato del servizio prod ─────────────────────────────
head_ "3. App Runner  (backend prod)"
if have aws; then
  arn=$(aws apprunner list-services --region "$REGION" \
        --query "ServiceSummaryList[?ServiceName=='$SERVICE'].ServiceArn" --output text 2>/dev/null || true)
  if [[ -n "$arn" ]]; then
    aws apprunner describe-service --service-arn "$arn" --region "$REGION" \
      --query 'Service.[ServiceName,Status,ServiceUrl]' --output text 2>/dev/null \
      | awk -F'\t' '{printf "  service: %s\n  status:  %s\n  url:     https://%s\n", $1,$2,$3}'
  else
    echo "  ✗ servizio '$SERVICE' non trovato"
  fi
fi

# ── 4. Endpoint pubblici + dashboard di terze parti ──────────────────────────
head_ "4. Endpoint & dashboard esterni"
cat <<'EOF'
  Sito          https://lauravatar.com                    (Cloudflare Worker)
  Cedric prod   https://meet-cedric.com
  Recall        https://us-west-2.recall.ai/dashboard      (bot in eu-central-1)
  Supabase      dashboard → project ref in .env (SUPABASE_PROJECT_REF)
  Stripe        https://dashboard.stripe.com               (key: /laura/prod/STRIPE_SECRET_KEY)
  Cloudflare    https://dash.cloudflare.com
  AWS console   https://eu-central-1.console.aws.amazon.com/apprunner
EOF

echo
dim "Suggerimento: './scripts/access.sh get /laura/prod/RECALL_API_KEY' per un singolo segreto."
