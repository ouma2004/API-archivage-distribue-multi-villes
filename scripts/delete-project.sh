#!/usr/bin/env bash
#
# delete-project.sh — Supprime un projet MinIO isolé
# et son entrée dans values.yaml.
#
# Usage :
#   ./scripts/delete-project.sh <alias_mc> <nom_projet> [chemin_values.yaml]
#
# Exemple :
#   ./scripts/delete-project.sh minio-casa projettest4 ./doc-archiver-chart/values.yaml
#
# Suppression :
#   - contenu du bucket
#   - bucket
#   - utilisateur MinIO
#   - policy MinIO
#   - entrée du projet dans values.yaml
#
# Sécurité :
#   1. Vérification du secret_key du projet
#   2. Confirmation DELETE-<projet>
#   3. Refus si plusieurs tokens correspondent au même nom
#

set -euo pipefail

# ============================================================
# 0. Vérification des arguments
# ============================================================

if [ "$#" -lt 2 ] || [ "$#" -gt 3 ]; then
    echo "Usage : $0 <alias_mc> <nom_projet> [chemin_values.yaml]" >&2
    exit 1
fi

ALIAS="$1"
PROJECT_NAME_RAW="$2"
VALUES_PATH="${3:-}"

# ============================================================
# 1. Validation du nom du projet
# ============================================================

PROJECT_NAME=$(echo "$PROJECT_NAME_RAW" | tr '[:upper:]' '[:lower:]')

if [[ ! "$PROJECT_NAME" =~ ^[a-z0-9-]+$ ]]; then
    echo "ERREUR : nom de projet invalide '$PROJECT_NAME_RAW'." >&2
    echo "Seuls les minuscules, chiffres et tirets sont autorisés." >&2
    exit 1
fi

if [ "$PROJECT_NAME" != "$PROJECT_NAME_RAW" ]; then
    echo "Info : nom converti en minuscules → '$PROJECT_NAME'"
fi

BUCKET="documents-${PROJECT_NAME}"
POLICY_NAME="${PROJECT_NAME}-policy"

# ============================================================
# 2. Vérification de l'alias MinIO
# ============================================================

echo "=== Vérification de l'alias '$ALIAS' ==="

if ! mc ls "$ALIAS" > /dev/null 2>&1; then
    echo "ERREUR : l'alias '$ALIAS' n'est pas configuré ou inaccessible." >&2
    exit 1
fi

echo "✓ Alias MinIO accessible."

# ============================================================
# 3. Recherche de Python
# ============================================================

PYTHON_BIN=""

if command -v python3 > /dev/null 2>&1; then
    PYTHON_BIN="python3"
elif command -v python > /dev/null 2>&1; then
    PYTHON_BIN="python"
elif command -v py > /dev/null 2>&1; then
    PYTHON_BIN="py"
fi

# ============================================================
# 4. Variables projet
# ============================================================

ACCESS_KEY=""
SECRET_KEY=""
PROJECT_TOKEN=""
MATCH_COUNT=0

BUCKET_EXISTS=false
USER_EXISTS=false
POLICY_EXISTS=false
VALUES_PROJECT_EXISTS=false

# ============================================================
# 5. Recherche du projet dans values.yaml
# ============================================================

if [ -n "$VALUES_PATH" ]; then

    if [ ! -f "$VALUES_PATH" ]; then
        echo ""
        echo "ERREUR : fichier values.yaml introuvable :"
        echo "  $VALUES_PATH"
        exit 1
    fi

    if [ -z "$PYTHON_BIN" ]; then
        echo ""
        echo "ERREUR : Python est nécessaire pour lire values.yaml."
        exit 1
    fi

    echo ""
    echo "=== Recherche du projet dans values.yaml ==="

    PROJECT_DATA=$(
        "$PYTHON_BIN" - "$VALUES_PATH" "$PROJECT_NAME" << 'PYEOF'
import json
import re
import sys

values_path, project_name = sys.argv[1:3]

with open(values_path, "r", encoding="utf-8") as f:
    content = f.read()

pattern = re.compile(
    r"(projectsJson:\s*')(\{.*?\})(')",
    re.DOTALL
)

match = pattern.search(content)

if not match:
    print("ERROR|projectsJson_not_found")
    sys.exit(0)

try:
    projects = json.loads(match.group(2))
except json.JSONDecodeError as e:
    print(f"ERROR|invalid_json|{e}")
    sys.exit(0)

matches = []

for token, project in projects.items():

    if not isinstance(project, dict):
        continue

    if str(project.get("name", "")).lower() == project_name.lower():

        matches.append({
            "token": token,
            "name": project.get("name", ""),
            "bucket": project.get("bucket", ""),
            "access_key": project.get("access_key", ""),
            "secret_key": project.get("secret_key", "")
        })

print("COUNT|" + str(len(matches)))

for item in matches:
    print(
        "PROJECT|"
        + item["token"] + "|"
        + item["name"] + "|"
        + item["bucket"] + "|"
        + item["access_key"] + "|"
        + item["secret_key"]
    )
PYEOF
    )

    if echo "$PROJECT_DATA" | grep -q '^ERROR|'; then

        ERROR_LINE=$(echo "$PROJECT_DATA" | grep '^ERROR|' | head -n 1)

        echo "ERREUR lors de la lecture de values.yaml :"
        echo "$ERROR_LINE"
        exit 1
    fi

    MATCH_COUNT=$(echo "$PROJECT_DATA" | grep '^COUNT|' | cut -d'|' -f2)

    # --------------------------------------------------------
    # Protection contre les doublons
    # --------------------------------------------------------

    if [ "$MATCH_COUNT" -gt 1 ]; then

        echo ""
        echo "============================================================"
        echo "🛑 SUPPRESSION REFUSÉE"
        echo "============================================================"
        echo ""
        echo "Plusieurs tokens correspondent au projet '$PROJECT_NAME'."
        echo ""
        echo "Tokens trouvés :"

        echo "$PROJECT_DATA" |
            grep '^PROJECT|' |
            while IFS='|' read -r _ token name bucket access secret; do
                echo "  - $token"
                echo "    name       : $name"
                echo "    bucket     : $bucket"
                echo "    access_key : $access"
            done

        echo ""
        echo "Pour des raisons de sécurité, aucune suppression"
        echo "ne sera effectuée."
        echo ""
        exit 1
    fi

    # --------------------------------------------------------
    # Un seul projet trouvé
    # --------------------------------------------------------

    if [ "$MATCH_COUNT" -eq 1 ]; then

        PROJECT_LINE=$(echo "$PROJECT_DATA" | grep '^PROJECT|' | head -n 1)

        IFS='|' read -r _ PROJECT_TOKEN PROJECT_FROM_VALUES BUCKET_FROM_VALUES ACCESS_KEY SECRET_KEY <<< "$PROJECT_LINE"

        VALUES_PROJECT_EXISTS=true

        echo "✓ Projet trouvé dans values.yaml."
        echo "  Token      : $PROJECT_TOKEN"
        echo "  Nom        : $PROJECT_FROM_VALUES"
        echo "  Bucket     : $BUCKET_FROM_VALUES"
        echo "  Access Key : $ACCESS_KEY"

        # ----------------------------------------------------
        # Vérification cohérence bucket
        # ----------------------------------------------------

        if [ "$BUCKET_FROM_VALUES" != "$BUCKET" ]; then
            echo ""
            echo "⚠️ ATTENTION : incohérence détectée."
            echo ""
            echo "Bucket attendu selon le nom du projet :"
            echo "  $BUCKET"
            echo ""
            echo "Bucket enregistré dans values.yaml :"
            echo "  $BUCKET_FROM_VALUES"
            echo ""

            echo "Pour éviter une suppression incorrecte,"
            echo "l'opération est refusée."
            exit 1
        fi

    else

        echo "ℹ Projet '$PROJECT_NAME' absent de values.yaml."

    fi

else

    echo ""
    echo "ℹ Aucun values.yaml fourni."
    echo "L'utilisateur ne pourra pas être déterminé automatiquement."

fi

# ============================================================
# 6. Vérification du bucket
# ============================================================

echo ""
echo "=== Vérification du projet '$PROJECT_NAME' ==="

if mc ls "${ALIAS}/${BUCKET}" > /dev/null 2>&1; then
    BUCKET_EXISTS=true
    echo "✓ Bucket trouvé : $BUCKET"
else
    echo "ℹ Bucket absent : $BUCKET"
fi

# ============================================================
# 7. Vérification utilisateur
# ============================================================

if [ -n "$ACCESS_KEY" ]; then

    if mc admin user info "$ALIAS" "$ACCESS_KEY" > /dev/null 2>&1; then
        USER_EXISTS=true
        echo "✓ Utilisateur MinIO trouvé : $ACCESS_KEY"
    else
        echo "ℹ Utilisateur MinIO déjà absent : $ACCESS_KEY"
    fi

else

    echo "ℹ Aucun utilisateur déterminé automatiquement."

fi

# ============================================================
# 8. Vérification policy
# ============================================================

if mc admin policy info "$ALIAS" "$POLICY_NAME" > /dev/null 2>&1; then
    POLICY_EXISTS=true
    echo "✓ Policy trouvée : $POLICY_NAME"
else
    echo "ℹ Policy absente : $POLICY_NAME"
fi

# ============================================================
# 9. Protection projet inexistant
# ============================================================

if [ "$BUCKET_EXISTS" = false ] && \
   [ "$USER_EXISTS" = false ] && \
   [ "$POLICY_EXISTS" = false ] && \
   [ "$VALUES_PROJECT_EXISTS" = false ]; then

    echo ""
    echo "============================================================"
    echo "Aucun élément correspondant au projet '$PROJECT_NAME'."
    echo "Aucune suppression effectuée."
    echo "============================================================"

    exit 0
fi

# ============================================================
# 10. Affichage récapitulatif
# ============================================================

echo ""
echo "============================================================"
echo "⚠️  SUPPRESSION DU PROJET"
echo "============================================================"
echo ""
echo "Ville / alias : $ALIAS"
echo "Projet        : $PROJECT_NAME"
echo "Bucket        : $BUCKET"
echo "Policy        : $POLICY_NAME"

if [ -n "$ACCESS_KEY" ]; then
    echo "Utilisateur   : $ACCESS_KEY"
else
    echo "Utilisateur   : non déterminé"
fi

if [ "$VALUES_PROJECT_EXISTS" = true ]; then
    echo "values.yaml   : projet trouvé"
else
    echo "values.yaml   : projet non trouvé"
fi

echo ""
echo "⚠️ Cette opération peut supprimer définitivement"
echo "   tous les documents du projet."
echo ""
echo "============================================================"
echo ""

# ============================================================
# 11. Vérification du secret du projet
# ============================================================

if [ -n "$SECRET_KEY" ]; then

    echo "=== Authentification du projet ==="
    echo ""
    echo "Une authentification est nécessaire avant la suppression."
    echo "Projet : $PROJECT_NAME"
    echo ""

    read -r -s -p "Secret du projet : " PROVIDED_SECRET
    echo ""

    if [ "$PROVIDED_SECRET" != "$SECRET_KEY" ]; then

        echo ""
        echo "❌ Secret incorrect."
        echo "Suppression annulée."
        exit 1
    fi

    unset PROVIDED_SECRET

    echo "✓ Secret correct."

else

    echo ""
    echo "⚠️ Aucun secret trouvé dans values.yaml."
    echo "La vérification par secret ne peut pas être effectuée."
    echo ""
    echo "Pour des raisons de sécurité, suppression refusée."
    exit 1

fi

# ============================================================
# 12. Confirmation forte
# ============================================================

echo ""
echo "⚠️ Dernière confirmation."
echo ""
echo "Cette opération est irréversible."
echo ""

read -r -p "Pour continuer, tape exactement DELETE-$PROJECT_NAME : " CONFIRMATION

if [ "$CONFIRMATION" != "DELETE-$PROJECT_NAME" ]; then

    echo ""
    echo "❌ Confirmation incorrecte."
    echo "Suppression annulée."
    exit 1

fi

echo ""
echo "✓ Confirmation acceptée."
echo ""

# ============================================================
# 13. Suppression du contenu du bucket
# ============================================================

if [ "$BUCKET_EXISTS" = true ]; then

    echo "=== Suppression des documents du bucket ==="

    if mc rm --recursive --force "${ALIAS}/${BUCKET}"; then
        echo "✓ Contenu du bucket supprimé."
    else
        echo ""
        echo "❌ ERREUR : impossible de supprimer le contenu du bucket."
        exit 1
    fi

else

    echo "ℹ Bucket déjà absent."

fi

# ============================================================
# 14. Suppression du bucket
# ============================================================

if [ "$BUCKET_EXISTS" = true ]; then

    echo ""
    echo "=== Suppression du bucket ==="

    if mc rb --force "${ALIAS}/${BUCKET}"; then
        echo "✓ Bucket supprimé."
    else
        echo ""
        echo "❌ ERREUR : impossible de supprimer le bucket."
        exit 1
    fi

fi

# ============================================================
# Suppression de la policy de l'utilisateur
# ============================================================

echo ""
echo "=== Détachement de la policy ==="

if [ "$USER_EXISTS" = true ] && [ -n "$ACCESS_KEY" ]; then

    if mc admin policy detach "$ALIAS" "$POLICY_NAME" --user "$ACCESS_KEY"; then
        echo "✓ Policy détachée de l'utilisateur."
    else
        echo "⚠️ Impossible de détacher la policy."
        exit 1
    fi

else
    echo "ℹ Aucun utilisateur à détacher."
fi


# ============================================================
# Suppression de l'utilisateur
# ============================================================

echo ""
echo "=== Suppression de l'utilisateur ==="

if [ "$USER_EXISTS" = true ]; then

    if mc admin user remove "$ALIAS" "$ACCESS_KEY"; then
        echo "✓ Utilisateur supprimé : $ACCESS_KEY"
    else
        echo "❌ Impossible de supprimer l'utilisateur."
        exit 1
    fi

else
    echo "ℹ Utilisateur déjà absent."
fi


# ============================================================
# Suppression de la policy
# ============================================================

echo ""
echo "=== Suppression de la policy ==="

if [ "$POLICY_EXISTS" = true ]; then

    if mc admin policy remove "$ALIAS" "$POLICY_NAME"; then
        echo "✓ Policy supprimée : $POLICY_NAME"
    else
        echo "❌ Impossible de supprimer la policy."
        exit 1
    fi

else
    echo "ℹ Policy déjà absente."
fi

# ============================================================
# 17. Suppression du projet dans values.yaml
# ============================================================

if [ "$VALUES_PROJECT_EXISTS" = true ]; then

    echo ""
    echo "=== Mise à jour de values.yaml ==="

    "$PYTHON_BIN" - "$VALUES_PATH" "$PROJECT_NAME" << 'PYEOF'

import json
import re
import sys

values_path, project_name = sys.argv[1:3]

with open(values_path, "r", encoding="utf-8") as f:
    content = f.read()

pattern = re.compile(
    r"(projectsJson:\s*')(\{.*?\})(')",
    re.DOTALL
)

match = pattern.search(content)

if not match:
    print("ERREUR : projectsJson introuvable.", file=sys.stderr)
    sys.exit(1)

try:
    current = json.loads(match.group(2))
except json.JSONDecodeError as e:
    print(f"ERREUR : projectsJson invalide : {e}", file=sys.stderr)
    sys.exit(1)

matches = []

for token, project in current.items():

    if not isinstance(project, dict):
        continue

    if str(project.get("name", "")).lower() == project_name.lower():
        matches.append(token)

# Sécurité : plusieurs projets identiques
if len(matches) > 1:

    print(
        f"ERREUR : plusieurs tokens correspondent à "
        f"'{project_name}'.",
        file=sys.stderr
    )

    sys.exit(1)

if len(matches) == 0:

    print(
        f"ERREUR : projet '{project_name}' absent de values.yaml.",
        file=sys.stderr
    )

    sys.exit(1)

token = matches[0]

# ------------------------------------------------------------
# Backup avant modification
# ------------------------------------------------------------

backup_path = values_path + ".bak"

with open(backup_path, "w", encoding="utf-8") as f:
    f.write(content)

# ------------------------------------------------------------
# Suppression
# ------------------------------------------------------------

del current[token]

new_json = json.dumps(
    current,
    ensure_ascii=False,
    separators=(",", ":")
)

new_content = (
    content[:match.start(2)]
    + new_json
    + content[match.end(2):]
)

with open(values_path, "w", encoding="utf-8") as f:
    f.write(new_content)

print(f"✓ Projet supprimé de values.yaml : {project_name}")
print(f"✓ Token supprimé : {token}")
print(f"✓ Backup créé : {backup_path}")

PYEOF

else

    echo ""
    echo "ℹ Aucun projet à supprimer dans values.yaml."

fi

# ============================================================
# 18. Vérification finale
# ============================================================

echo ""
echo "============================================================"
echo "=== Vérification finale ==="
echo "============================================================"

FAILED=false

# ------------------------------------------------------------
# Bucket
# ------------------------------------------------------------

if mc ls "${ALIAS}/${BUCKET}" > /dev/null 2>&1; then

    echo "❌ Bucket existe encore : $BUCKET"
    FAILED=true

else

    echo "✓ Bucket absent."

fi

# ------------------------------------------------------------
# Utilisateur
# ------------------------------------------------------------

if [ -n "$ACCESS_KEY" ]; then

    if mc admin user info "$ALIAS" "$ACCESS_KEY" > /dev/null 2>&1; then

        echo "❌ Utilisateur existe encore : $ACCESS_KEY"
        FAILED=true

    else

        echo "✓ Utilisateur absent."

    fi

fi

# ------------------------------------------------------------
# Policy
# ------------------------------------------------------------

if mc admin policy info "$ALIAS" "$POLICY_NAME" > /dev/null 2>&1; then

    echo "❌ Policy existe encore : $POLICY_NAME"
    FAILED=true

else

    echo "✓ Policy absente."

fi

# ------------------------------------------------------------
# values.yaml
# ------------------------------------------------------------

if [ "$VALUES_PROJECT_EXISTS" = true ]; then

    VALUES_STILL_EXISTS=$(
        "$PYTHON_BIN" - "$VALUES_PATH" "$PROJECT_NAME" << 'PYEOF'
import json
import re
import sys

values_path, project_name = sys.argv[1:3]

with open(values_path, "r", encoding="utf-8") as f:
    content = f.read()

pattern = re.compile(
    r"(projectsJson:\s*')(\{.*?\})(')",
    re.DOTALL
)

match = pattern.search(content)

if not match:
    print("ERROR")
    sys.exit(0)

try:
    projects = json.loads(match.group(2))
except Exception:
    print("ERROR")
    sys.exit(0)

for project in projects.values():

    if (
        isinstance(project, dict)
        and str(project.get("name", "")).lower()
        == project_name.lower()
    ):
        print("FOUND")
        sys.exit(0)

print("ABSENT")
PYEOF
    )

    if [ "$VALUES_STILL_EXISTS" = "FOUND" ]; then

        echo "❌ Projet existe encore dans values.yaml."
        FAILED=true

    else

        echo "✓ Projet absent de values.yaml."

    fi

fi

# ============================================================
# 19. Résultat final
# ============================================================

echo ""

if [ "$FAILED" = true ]; then

    echo "============================================================"
    echo "❌ SUPPRESSION INCOMPLÈTE"
    echo "============================================================"
    echo ""
    echo "Le projet '$PROJECT_NAME' n'a pas été entièrement supprimé."
    echo "Vérifie les éléments signalés ci-dessus."
    exit 1

else

    echo "============================================================"
    echo "✓ PROJET '$PROJECT_NAME' SUPPRIMÉ AVEC SUCCÈS"
    echo "============================================================"
    echo ""
    echo "Éléments supprimés :"
    echo "  ✓ Documents"
    echo "  ✓ Bucket"
    echo "  ✓ Utilisateur"
    echo "  ✓ Policy"
    
    if [ "$VALUES_PROJECT_EXISTS" = true ]; then
        echo "  ✓ Entrée values.yaml"
        echo "  ✓ Backup values.yaml.bak"
    fi

    echo ""

fi
