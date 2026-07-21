#!/usr/bin/env bash
#
# create-project.sh — Crée un nouveau projet (bucket + compte MinIO isolé)
# sur UNE ville donnée, et met à jour values.yaml automatiquement.
#
# Usage :
#   ./scripts/create-project.sh <alias_mc> <nom_projet> <access_key> <secret_key> [chemin_values.yaml]
#
# Exemple :
#   ./scripts/create-project.sh minio-casa projetalsa adminalsa "SecretProjetAlsa123" ./doc-archiver-chart/values.yaml
#
# Si le 5e argument (chemin values.yaml) est omis, le script affiche juste
# le fragment JSON à copier à la main — comportement identique à avant.
#
# Prérequis : l'alias mc correspondant doit déjà exister, et python3 doit
# être installé (présent par défaut sur la VM Azure et dans Git Bash/WSL).

set -euo pipefail

if [ "$#" -lt 4 ] || [ "$#" -gt 5 ]; then
    echo "Usage : $0 <alias_mc> <nom_projet> <access_key> <secret_key> [chemin_values.yaml]" >&2
    exit 1
fi

ALIAS="$1"
PROJECT_NAME_RAW="$2"
ACCESS_KEY="$3"
SECRET_KEY="$4"
VALUES_PATH="${5:-}"

# ── Validation stricte du nom (règles S3 : minuscules, chiffres, tirets) ──
PROJECT_NAME=$(echo "$PROJECT_NAME_RAW" | tr '[:upper:]' '[:lower:]')
if [[ ! "$PROJECT_NAME" =~ ^[a-z0-9-]+$ ]]; then
    echo "ERREUR : nom de projet invalide '$PROJECT_NAME_RAW'." >&2
    echo "Seuls les minuscules, chiffres et tirets sont autorisés (règle S3)." >&2
    exit 1
fi
if [ "$PROJECT_NAME" != "$PROJECT_NAME_RAW" ]; then
    echo "Info : nom converti en minuscules → '$PROJECT_NAME'"
fi

BUCKET="documents-${PROJECT_NAME}"
POLICY_NAME="${PROJECT_NAME}-policy"

echo "=== Vérification de l'alias '$ALIAS' ==="
if ! mc ls "$ALIAS" > /dev/null 2>&1; then
    echo "ERREUR : l'alias '$ALIAS' n'est pas configuré ou injoignable." >&2
    echo "Configure-le d'abord : mc alias set $ALIAS <url> <access_key_root> <secret_key_root>" >&2
    exit 1
fi

# ── Idempotent : ne plante pas si le bucket existe déjà ─────────────────
echo "=== Bucket '$BUCKET' ==="
if mc ls "${ALIAS}/${BUCKET}" > /dev/null 2>&1; then
    echo "Bucket déjà existant, on continue."
else
    mc mb "${ALIAS}/${BUCKET}"
fi

# ── Idempotent : ne plante pas si l'utilisateur existe déjà ─────────────
echo "=== Utilisateur '$ACCESS_KEY' ==="
if mc admin user info "$ALIAS" "$ACCESS_KEY" > /dev/null 2>&1; then
    echo "Utilisateur déjà existant, on continue (mot de passe non modifié)."
else
    mc admin user add "$ALIAS" "$ACCESS_KEY" "$SECRET_KEY"
fi

# ── Policy : recréée à chaque fois (idempotent par nature avec mc) ──────
echo "=== Policy '$POLICY_NAME' ==="
POLICY_FILE=$(mktemp)
cat > "$POLICY_FILE" << EOF
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": ["s3:*"],
    "Resource": ["arn:aws:s3:::${BUCKET}", "arn:aws:s3:::${BUCKET}/*"]
  }]
}
EOF
mc admin policy create "$ALIAS" "$POLICY_NAME" "$POLICY_FILE"
mc admin policy attach "$ALIAS" "$POLICY_NAME" --user "$ACCESS_KEY"
rm -f "$POLICY_FILE"

# ── Token (réutilise un token déjà généré pour ce projet si possible) ───
TOKEN="tok_${PROJECT_NAME}_$(head -c4 /dev/urandom | xxd -p)"

echo ""
echo "=== Projet '$PROJECT_NAME' créé sur '$ALIAS' ==="

# ── Mise à jour automatique de values.yaml (si chemin fourni) ──────────
PYTHON_BIN=""
if command -v python3 > /dev/null 2>&1; then
    PYTHON_BIN="python3"
elif command -v python > /dev/null 2>&1; then
    PYTHON_BIN="python"
elif command -v py > /dev/null 2>&1; then
    PYTHON_BIN="py"
fi

if [ -n "$VALUES_PATH" ] && [ -n "$PYTHON_BIN" ]; then
    if [ ! -f "$VALUES_PATH" ]; then
        echo "ERREUR : fichier '$VALUES_PATH' introuvable." >&2
        exit 1
    fi

    "$PYTHON_BIN" - "$VALUES_PATH" "$TOKEN" "$PROJECT_NAME" "$BUCKET" "$ACCESS_KEY" "$SECRET_KEY" << 'PYEOF'
import re
import json
import sys

values_path, token, name, bucket, access_key, secret_key = sys.argv[1:7]

with open(values_path, "r", encoding="utf-8") as f:
    content = f.read()

# Cherche la ligne projectsJson: '{...}' (JSON entre guillemets simples)
pattern = re.compile(r"(projectsJson:\s*')(\{.*?\})(')", re.DOTALL)
match = pattern.search(content)
if not match:
    print("ERREUR : 'projectsJson' introuvable dans values.yaml — "
          "ajoute la ligne manuellement une première fois.", file=sys.stderr)
    sys.exit(1)

try:
    current = json.loads(match.group(2))
except json.JSONDecodeError as e:
    print(f"ERREUR : projectsJson existant n'est pas un JSON valide : {e}", file=sys.stderr)
    sys.exit(1)

if token in current:
    print(f"ERREUR : le token '{token}' existe déjà — rien changé.", file=sys.stderr)
    sys.exit(1)

current[token] = {
    "name": name, "bucket": bucket,
    "access_key": access_key, "secret_key": secret_key,
}
new_json_str = json.dumps(current, ensure_ascii=False)

new_content = content[:match.start(2)] + new_json_str + content[match.end(2):]

backup_path = values_path + ".bak"
with open(backup_path, "w", encoding="utf-8") as f:
    f.write(content)

with open(values_path, "w", encoding="utf-8") as f:
    f.write(new_content)

print(f"OK : token '{token}' ajouté dans {values_path}")
print(f"Sauvegarde de l'ancienne version : {backup_path}")
PYEOF

    echo ""
    echo "⚠️  N'oublie pas de relancer 'helm upgrade' pour que l'API prenne"
    echo "    en compte ce nouveau token."
else
    if [ -n "$VALUES_PATH" ] && [ -z "$PYTHON_BIN" ]; then
        echo ""
        echo "⚠️  Python introuvable — mise à jour automatique de values.yaml"
        echo "    impossible. Ajoute ce fragment À LA MAIN :"
    else
        echo ""
        echo "Ajoute ceci dans projectsJson (values.yaml), à la main :"
    fi
    echo ""
    echo "\"${TOKEN}\":{\"name\":\"${PROJECT_NAME}\",\"bucket\":\"${BUCKET}\",\"access_key\":\"${ACCESS_KEY}\",\"secret_key\":\"${SECRET_KEY}\"}"
fi

