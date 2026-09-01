#!/bin/sh
set -eu

required_vars="MISTRAL_API_KEY SUPABASE_URL SUPABASE_SERVICE_ROLE_KEY TELEGRAM_BOT_TOKEN"
for var_name in $required_vars; do
  eval "var_value=\${$var_name:-}"
  if [ -z "$var_value" ]; then
    echo "ERROR: falta la variable obligatoria $var_name" >&2
    exit 78
  fi
done

if [ -z "${TELEGRAM_ALLOWED_USERS:-}" ]; then
  echo "ERROR: TELEGRAM_ALLOWED_USERS no puede estar vacío en producción." >&2
  echo "Define IDs numéricos separados por comas." >&2
  exit 78
fi

if [ -n "${TELEGRAM_ALLOWED_USERS:-}" ] && ! printf '%s\n' "$TELEGRAM_ALLOWED_USERS" \
  | grep -Eq '^[[:space:]]*[0-9]+([[:space:]]*,[[:space:]]*[0-9]+)*[[:space:]]*$'; then
  echo "ERROR: TELEGRAM_ALLOWED_USERS solo admite IDs numéricos separados por comas." >&2
  exit 78
fi

exec "$@"
