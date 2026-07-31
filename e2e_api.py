# -*- coding: utf-8 -*-
"""
e2e_api.py — Orchestrateur E2E pour une API HTTP (doc-archiver)
================================================================

Meme architecture "token-efficient" que e2e_v2.py (projets Odoo), mais adaptee
a une API REST SANS interface : l'agent teste des ENDPOINTS HTTP (pas de
navigateur, pas d'UI). L'execution se fait en Playwright `request` (HTTP pur).

Les 3 passes sont identiques dans l'esprit a e2e_v2 :
  - PASSE 1 (--analyze)            : Claude lit les sources Python et produit un
                                     CONTRAT JSON (liste d'endpoints testables)
                                     + un RESUME lisible avec des PISTES.
  - PASSE 2a (--generate-scenarios): Claude genere le plan de scenarios des
                                     PISTES choisies (sans executer).
  - PASSE 2b (--run)               : Claude ecrit ET execute des specs Playwright
                                     `request` contre l'API, puis produit un
                                     rapport (RAPPORT_LIVE possible), poste en issue.

Le choix des pistes se fait comme dans e2e_v2 : via le formulaire (form_server.py
+ ngrok) qui ecrit contrats/<api>_reponses.md (section "=== PISTES CHOISIES ===").

Difference cle avec l'Odoo :
  - Pas d'UI, pas de login web, pas de MCP navigateur. Tout est HTTP.
  - L'isolation multi-tenant (403 cross-projet) est du FONCTIONNEL ici (c'est le
    coeur metier), donc INCLUSE. On exclut seulement les vraies attaques
    (injection SQL, forcer des chemins hors API).

Usage :
  python e2e_api.py --analyze
  python e2e_api.py --generate-scenarios --pistes=isolation,auth
  python e2e_api.py --run --pistes=isolation --max-scenarios=30
"""

import subprocess
import sys
import os
import json
import shutil
from datetime import datetime

try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))
except Exception:
    pass

# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

ROOT = os.path.dirname(os.path.abspath(__file__))

# Nom logique de l'API (sert de prefixe pour tous les fichiers generes).
API_NAME = os.environ.get("API_NAME", "doc-archiver")

# Fichiers sources a analyser (passe 1). Le pipeline vit DANS le repo
# doc-archiver, donc les sources sont a la racine (SOURCE_DIR=.).
# FICHIERS_EXCLUS evite que l'agent s'analyse lui-meme.
SOURCE_DIR = os.environ.get("SOURCE_DIR", ROOT)
FICHIERS_SOURCES = ["main.py", "archiver.py", "database.py", "config.py", "replicator.py"]
FICHIERS_EXCLUS = {"e2e_api.py", "form_server.py"}   # ne jamais s'analyser soi-meme

CONTRATS_DIR = os.path.join(ROOT, "contrats")
REPORTS_DIR = os.path.join(ROOT, "reports")

# ── Cible de l'API (equivalent de ODOO_URL). Local par defaut ; en CI le compose
#    expose sur localhost:30800. Aucun ngrok cote execution : appels HTTP sortants.
BASE_URL = os.environ.get("BASE_URL", "http://localhost:30800").rstrip("/")
VILLE = os.environ.get("VILLE", "casa")

# ── Tokens de test (injectes par le compose / secrets). Passes a Claude pour
#    qu'il les utilise dans les specs. Ce sont des tokens de TEST, pas de prod.
TOKENS = {
    "A": os.environ.get("TOKEN_A", "test-token-a"),
    "B": os.environ.get("TOKEN_B", "test-token-b"),
    "ADMIN": os.environ.get("ADMIN_TOKEN", "test-admin-token"),
    "LEGACY": os.environ.get("LEGACY_TOKEN", "test-legacy-token"),
}

# ── GitHub (poster le rapport en issue) ───────────────────────────────────────
GITHUB_REPO = os.environ.get("GITHUB_REPO", "mohammedaminedahmani-tech/doc-archiver")
GITHUB_ISSUE_NUMBER = int(os.environ.get("GITHUB_ISSUE_NUMBER", "1"))

# ── Rapport en direct (comme e2e_v2) ──────────────────────────────────────────
RAPPORT_LIVE = os.environ.get("RAPPORT_LIVE", "false").lower() == "true"


# ══════════════════════════════════════════════════════════════════════════════
# COLLECTE DES SOURCES
# ══════════════════════════════════════════════════════════════════════════════

def get_fichiers_sources():
    """Liste les fichiers sources presents (chemins relatifs a SOURCE_DIR)."""
    presents = []
    for f in FICHIERS_SOURCES:
        if f in FICHIERS_EXCLUS:
            continue
        chemin = os.path.join(SOURCE_DIR, f)
        if os.path.exists(chemin):
            presents.append(f)
        else:
            print(f"[e2e_api] ⚠️  Source absente (ignoree) : {chemin}")
    if not presents:
        print(f"[e2e_api] ❌ Aucune source trouvee dans {SOURCE_DIR}.")
        print(f"[e2e_api] Definis SOURCE_DIR vers le repo doc-archiver "
              f"(qui contient main.py, config.py, ...).")
        sys.exit(1)
    print(f"[e2e_api] Sources analysees ({len(presents)}) : {', '.join(presents)}")
    return presents


# ══════════════════════════════════════════════════════════════════════════════
# CLAUDE CODE CLI  (identique a e2e_v2)
# ══════════════════════════════════════════════════════════════════════════════

def trouver_claude():
    return (
        shutil.which('claude.cmd')
        or shutil.which('claude')
        or os.path.join(os.environ.get('APPDATA', ''), 'npm', 'claude.cmd')
    )


def appeler_claude(prompt, timeout=1800, cwd=None):
    """
    Lance Claude Code CLI en mode non-interactif. cwd = dossier ou Claude peut
    LIRE/ECRIRE des fichiers. Pour --analyze/--generate on se place dans ROOT
    (contrats/ y est). Pour --run on se place dans ROOT aussi (specs + reports).
    Le prompt passe par STDIN (pas d'argument : evite 'Argument list too long').
    """
    claude_exe = trouver_claude()
    if not claude_exe:
        print("[e2e_api] ERREUR : Claude Code CLI introuvable.")
        print("[e2e_api] Installez : npm i -g @anthropic-ai/claude-code")
        sys.exit(1)

    try:
        proc = subprocess.Popen(
            [claude_exe, '-p',
             '--dangerously-skip-permissions', '--output-format', 'json'],
            cwd=cwd or ROOT,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding='utf-8',
        )
        print("[e2e_api] ⏳ Claude travaille (peut prendre plusieurs minutes)...")
        stdout, stderr = proc.communicate(input=prompt, timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        print(f"[e2e_api] TIMEOUT apres {timeout}s")
        return "__TIMEOUT__"

    if not stdout:
        if stderr:
            print(f"[e2e_api] STDERR : {stderr[:500]}")
        return None

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        print("[e2e_api] Impossible de parser la sortie JSON de Claude.")
        print(stdout[:500])
        return None

    cout = data.get('total_cost_usd', 0)
    print(f"[e2e_api] ✅ Termine — cout : ${cout:.4f}")
    usage = data.get('usage')
    if usage:
        print(f"[e2e_api] 📊 Tokens : {json.dumps(usage, ensure_ascii=False)}")

    return data.get('result', '').strip()


def appeler_claude_avec_retry(prompt, timeout=1800, max_retries=3, cwd=None):
    """Retry + backoff. Ne relance JAMAIS un prompt geant apres timeout."""
    import time
    derniere_erreur = None
    for tentative in range(1, max_retries + 1):
        try:
            resultat = appeler_claude(prompt, timeout=timeout, cwd=cwd)
            if resultat == "__TIMEOUT__":
                print(f"[e2e_api] ❌ Timeout — PAS de nouvelle tentative "
                      f"(evite de doubler le cout). Reduis --max-scenarios.")
                return None
            if resultat:
                return resultat
            derniere_erreur = "reponse vide"
        except SystemExit:
            raise
        except Exception as e:
            derniere_erreur = str(e)

        if tentative < max_retries:
            attente = 20 * tentative
            print(f"[e2e_api] ⚠️  Tentative {tentative}/{max_retries} echouee "
                  f"({derniere_erreur}) — retry dans {attente}s...")
            time.sleep(attente)
        else:
            print(f"[e2e_api] ❌ Echec apres {max_retries} tentatives ({derniere_erreur}).")
    return None


def _nom_fichier(suffixe, groupe=None):
    if groupe:
        return f"{API_NAME}_{groupe}_{suffixe}"
    return f"{API_NAME}_{suffixe}"


# ══════════════════════════════════════════════════════════════════════════════
# PROMPT PASSE 1 — CONTRAT (endpoints) + RESUME + PISTES
# ══════════════════════════════════════════════════════════════════════════════

def construire_prompt_passe1(fichiers):
    liste = "\n".join(f"  - {os.path.join(SOURCE_DIR, f)}" for f in fichiers)
    return f"""Tu es un ingenieur QA senior expert des APIs HTTP (FastAPI). Ta mission :
analyser le code source de l'API "{API_NAME}" et produire un CONTRAT DE TEST
structure, en vue d'un test fonctionnel exhaustif de ses ENDPOINTS.

Cette API n'a AUCUNE interface graphique : c'est une API REST pure. Tout se
teste par des requetes HTTP (methodes, chemins, query params, corps, headers
d'authentification, codes de statut, corps de reponse).

== FICHIERS A LIRE (lis CHACUN avec ton outil de lecture) ==
{liste}

== CONTEXTE METIER (important) ==
- L'API stocke des documents par ville (query param `ville`), avec isolation
  multi-tenant par PROJET : un token = un projet = un bucket. Un projet ne doit
  JAMAIS acceder aux documents d'un autre projet.
- 4 modes d'authentification (Authorization: Bearer <token>) : token PROJET
  (voit son bucket), token ADMIN (voit tout), token LEGACY (bucket global
  "documents"), token INTER-SITES (reservE aux endpoints /api/replicate/*).
- L'isolation entre projets est le COEUR FONCTIONNEL : les tests 403 cross-projet
  sont du test FONCTIONNEL (pas un audit securite a exclure).
- Les endpoints /api/replicate/* (replication inter-sites) sont HORS PERIMETRE :
  ne genere AUCUN scenario dessus (deja valides par ailleurs).

== REGLES D'ANALYSE ==
1. IGNORE tout code COMMENTE. Ne considere que la logique ACTIVE.
2. Pour chaque endpoint, deduis du code : la methode HTTP, le chemin, les
   parametres (query/body), quels modes d'auth sont acceptes, les codes de
   statut possibles ET leur cause exacte (200/400/401/403/404/410/422/503...),
   et les regles d'isolation (quand renvoie-t-il 403 ?).
3. "source_method" : nom EXACT de la fonction Python qui implemente l'endpoint
   (ex: "get_document", "_check_ownership", "verify_token"). Jamais un nom de
   fichier. Si la regle vient d'une fonction transverse, cite-la.

== CE QUE TU DOIS PRODUIRE (ECRIS DIRECTEMENT 2 FICHIERS) ==
N'affiche PAS le contenu dans ta reponse. Utilise ton outil d'ecriture pour
CREER ces 2 fichiers (le dossier "contrats/" existe deja) :

FICHIER 1 : contrats/{API_NAME}_contrat.json
  -> CONTRAT en JSON pur, valide. Schema attendu :

{{
  "api": "{API_NAME}",
  "base_url_hint": "http://localhost:30800",
  "auth_modes": ["project", "admin", "legacy", "inter_site"],
  "endpoints": [
    {{
      "method": "GET|POST|PATCH|DELETE",
      "path": "/documents/{{doc_id}}",
      "summary": "...",
      "query_params": [{{"name": "ville", "required": true, "note": "..."}}],
      "body": "description du corps attendu ou null",
      "auth_accepted": ["project", "admin", "legacy"],
      "status_codes": {{"200": "cause", "403": "cause", "404": "cause"}},
      "isolation_rule": "quand renvoie-t-il 403 cross-projet, ou null",
      "source_method": "<fonction Python>",
      "security_critical": true,
      "in_scope": true,
      "needs_human_check": "<question ou absent>"
    }}
  ],
  "cross_cutting_rules": [
    {{"rule": "verify_token: 401 si token inconnu", "source_method": "verify_token"}},
    {{"rule": "_check_ownership: 403 si project_bucket != bucket appelant", "source_method": "_check_ownership"}}
  ],
  "open_questions": ["<questions transverses ; MAX 8>"]
}}

Regles JSON :
- Mets "in_scope": false pour les endpoints /api/replicate/* (hors perimetre).
- security_critical: true pour tout ce qui touche a l'isolation ou l'auth.

FICHIER 2 : contrats/{API_NAME}_resume.md  (format EXACT) :

=== RESUME — API {API_NAME} ===
(2-4 phrases : ce que fait l'API, combien d'endpoints, ce qui est testable)

ENDPOINTS PRINCIPAUX :
- <METHODE> <chemin> : <ce qu'il fait en 1 ligne>

=== MES QUESTIONS (reponds sous chaque question) ===
Q1. ...
   ->
Q2. ...
   ->

=== ZONE LIBRE — ajoute / corrige / precise ce que tu veux ===
(laisse cette zone vide, l'humain la remplira)

=== PISTES ===
(Regroupe les endpoints testables en PISTES logiques que TU deduis du code.
Pour une API d'archivage multi-tenant, ce sera typiquement : isolation
(cross-projet, 403), auth (4 modes, 401, /whoami), crud (upload/list/get/
rename/delete), edge_cases (fichier vide, ville invalide, id malforme).
Deduis-les toi-meme, il peut y en avoir 2 a 6. UNE piste par ligne, format
EXACT "id|nom|depends_on" ou :
  - id  = identifiant court minuscule sans espace (ex: isolation, auth, crud)
  - nom = libelle lisible court
  - depends_on = ids d'autres pistes requises avant (vide si aucune). Ex: la
    piste "isolation" depend de "crud" s'il faut d'abord uploader un doc pour
    tester l'acces cross-projet. Ne force pas une dependance par prudence.
Exemple :
crud|CRUD documents (upload/list/get/rename/delete)|
auth|Authentification (4 modes, 401, /whoami)|
isolation|Isolation multi-tenant (403 cross-projet)|crud
edge_cases|Cas limites (fichier vide, ville invalide)|crud
N'ajoute rien d'autre : seulement les lignes "id|nom|depends_on".)

== APRES AVOIR ECRIT LES 2 FICHIERS ==
Reponds UNIQUEMENT par une ligne de confirmation, ex :
"OK - 2 fichiers ecrits : contrats/{API_NAME}_contrat.json (N endpoints) et contrats/{API_NAME}_resume.md"
N'affiche NI le JSON NI le resume dans ta reponse.
""".strip()


# ══════════════════════════════════════════════════════════════════════════════
# EXTRACTION JSON ROBUSTE  (repris de e2e_v2)
# ══════════════════════════════════════════════════════════════════════════════

def extraire_json(texte):
    t = texte.replace('```json', '').replace('```JSON', '').replace('```', '')
    debut = t.find('{')
    if debut == -1:
        return None, "Aucun '{' trouve."
    profondeur = 0
    dans_chaine = False
    echappe = False
    for i in range(debut, len(t)):
        c = t[i]
        if dans_chaine:
            if echappe:
                echappe = False
            elif c == '\\':
                echappe = True
            elif c == '"':
                dans_chaine = False
            continue
        if c == '"':
            dans_chaine = True
        elif c == '{':
            profondeur += 1
        elif c == '}':
            profondeur -= 1
            if profondeur == 0:
                bloc = t[debut:i + 1]
                try:
                    return json.loads(bloc), None
                except json.JSONDecodeError as e:
                    return None, f"JSON invalide : {e}"
    return None, "Accolade fermante manquante."


def traiter_reponse_passe1(reponse):
    os.makedirs(CONTRATS_DIR, exist_ok=True)
    contrat_path = os.path.join(CONTRATS_DIR, f"{API_NAME}_contrat.json")
    resume_path = os.path.join(CONTRATS_DIR, f"{API_NAME}_resume.md")

    if os.path.exists(contrat_path):
        try:
            with open(contrat_path, encoding="utf-8") as f:
                contrat = json.load(f)
            nb = len(contrat.get("endpoints", []))
            nbq = len(contrat.get("open_questions", []))
            print(f"[e2e_api] ✅ Contrat ecrit par Claude : {contrat_path}")
            print(f"[e2e_api]    -> {nb} endpoint(s), {nbq} question(s)")
            resume = ""
            if os.path.exists(resume_path):
                with open(resume_path, encoding="utf-8") as f:
                    resume = f.read().strip()
                print(f"[e2e_api] ✅ Resume ecrit : {resume_path}")
            else:
                print("[e2e_api] ⚠️  Resume manquant.")
            return resume, contrat
        except json.JSONDecodeError as e:
            print(f"[e2e_api] ⚠️  Contrat present mais JSON invalide : {e}")

    print("[e2e_api] ℹ️  Contrat absent — extraction depuis la reponse.")
    contrat, err = extraire_json(reponse)
    if contrat is None:
        print(f"[e2e_api] ❌ Contrat non extractible : {err}")
        brut = os.path.join(CONTRATS_DIR, f"{API_NAME}_brut.txt")
        with open(brut, "w", encoding="utf-8") as f:
            f.write(reponse)
        print(f"[e2e_api] Reponse brute : {brut}")
        return "", None
    with open(contrat_path, "w", encoding="utf-8") as f:
        json.dump(contrat, f, indent=2, ensure_ascii=False)
    print(f"[e2e_api] ✅ Contrat recupere par extraction ({len(contrat.get('endpoints', []))} endpoints)")
    return "", contrat


def analyser_api(force=True):
    contrat_path = os.path.join(CONTRATS_DIR, f"{API_NAME}_contrat.json")
    if not force and os.path.exists(contrat_path):
        mtime = datetime.fromtimestamp(os.path.getmtime(contrat_path)).strftime('%Y-%m-%d %H:%M')
        print(f"\n[e2e_api] ♻️  Reutilisation du Contrat existant (genere le {mtime})")
        print(f"[e2e_api] Relance avec --analyze (sans --reuse) pour regenerer.")
        return

    print(f"\n[e2e_api] ═══ PASSE 1 — Analyse de l'API '{API_NAME}' ═══\n")
    fichiers = get_fichiers_sources()
    prompt = construire_prompt_passe1(fichiers)
    reponse = appeler_claude_avec_retry(prompt)
    if not reponse:
        print("[e2e_api] ❌ Pas de reponse exploitable.")
        return
    resume, contrat = traiter_reponse_passe1(reponse)
    print("\n" + "=" * 70)
    print("RESUME (a valider via le formulaire) :")
    print("=" * 70)
    print(resume)
    print("=" * 70)


# ══════════════════════════════════════════════════════════════════════════════
# CHARGEMENT CONTRAT + REPONSES (formulaire)
# ══════════════════════════════════════════════════════════════════════════════

def charger_contrat():
    contrat_path = os.path.join(CONTRATS_DIR, f"{API_NAME}_contrat.json")
    if not os.path.exists(contrat_path):
        print(f"[e2e_api] ❌ Contrat introuvable : {contrat_path}")
        print(f"[e2e_api] Lance d'abord : python e2e_api.py --analyze")
        return None, None
    with open(contrat_path, encoding='utf-8') as f:
        contrat = json.load(f)
    reponses = None
    reponses_path = os.path.join(CONTRATS_DIR, f"{API_NAME}_reponses.md")
    if os.path.exists(reponses_path):
        with open(reponses_path, encoding='utf-8') as f:
            reponses = f.read().strip()
        if reponses:
            print(f"[e2e_api] Reponses humaines chargees : {reponses_path}")
    return contrat, reponses


# ══════════════════════════════════════════════════════════════════════════════
# PROMPT PASSE 2a — SCENARIOS
# ══════════════════════════════════════════════════════════════════════════════

def _pistes_choisies(reponses, pistes_cible):
    if pistes_cible:
        return ", ".join(pistes_cible)
    if reponses and "=== PISTES CHOISIES ===" in reponses:
        apres = reponses.split("=== PISTES CHOISIES ===", 1)[1]
        lignes = [l.strip() for l in apres.splitlines() if l.strip()]
        return ", ".join(lignes)
    return ""


def construire_prompt_scenarios(contrat, reponses, pistes_cible=None,
                                 priority_only=False, max_scenarios=0):
    contrat_json = json.dumps(contrat, indent=2, ensure_ascii=False)
    pistes = _pistes_choisies(reponses, pistes_cible)

    bloc_reponses = ""
    if reponses:
        bloc_reponses = f"""
== REPONSES / CORRECTIONS DE L'HUMAIN (FONT AUTORITE) ==
Si elles contredisent ta comprehension du code, SUIS L'HUMAIN.
{reponses}
"""

    bloc_pistes = ""
    if pistes:
        bloc_pistes = f"""
== PERIMETRE CIBLE — PISTES CHOISIES (IMPERATIF) ==
La personne a choisi de ne tester QUE la/les piste(s) suivante(s) :
  {pistes}
Genere UNIQUEMENT les scenarios de ces pistes. Ignore tout le reste.
"""

    bloc_plafond = ""
    if max_scenarios:
        bloc_plafond = f"\n== PLAFOND ==\nLimite-toi a environ {max_scenarios} scenarios au total.\n"

    if priority_only:
        couverture = """Couvre UNIQUEMENT l'essentiel :
  - Chaque regle d'ISOLATION (acces cross-projet qui DOIT renvoyer 403).
  - Chaque code d'erreur d'auth (401 token invalide/absent).
  - Chaque erreur attendue documentee (400 fichier vide, 400 ville invalide,
    404 doc inexistant, 410 doc supprime, 422 param malforme).
N'inclus PAS les cas nominaux triviaux ni la section "non evidents"."""
    else:
        couverture = """Couvre systematiquement, pour chaque endpoint IN SCOPE (in_scope=true) :
  - Le cas nominal (2xx) avec un corps/params valides.
  - Chaque code de statut documente dans status_codes : un scenario qui le
    provoque (400, 401, 403, 404, 410, 422, 503 quand applicable).
  - Chaque regle d'isolation (isolation_rule) : un scenario cross-projet qui
    doit renvoyer 403, ET la variante qui doit reussir pour le bon projet.
  - Le comportement des 4 modes d'auth la ou c'est pertinent (project/admin/
    legacy ; inter_site est HORS scope).

Ajoute une section "non evidents" : echec partiel d'upload multi-fichiers,
double suppression, id devine d'un autre projet, path arbitraire, valeurs
limites — que tu deduis toi-meme (dans le perimetre des pistes choisies)."""

    return f"""Tu es un ingenieur QA senior specialiste des APIs HTTP. Voici le CONTRAT DE
TEST de l'API "{API_NAME}" (deduit du code). Ta mission : produire le PLAN de
scenarios de test HTTP, SANS les executer maintenant.
{bloc_plafond}
== CONTRAT DE TEST (source de verite) ==
{contrat_json}
{bloc_reponses}{bloc_pistes}
== CE QUE TU DOIS FAIRE ==
{"Limite-toi STRICTEMENT aux pistes cochees." if pistes else "Genere TOUS les scenarios in-scope."}

{couverture}

== PERIMETRE : QA FONCTIONNELLE D'UNE API (PAS D'UI) ==
Cette API n'a pas d'interface : chaque scenario est une REQUETE HTTP. Un
scenario = methode + chemin + params + headers d'auth + corps -> code de statut
attendu + verification du corps. L'isolation multi-tenant (403 cross-projet) est
du FONCTIONNEL, teste-la a fond. Reste sur les endpoints prevus ; n'essaie PAS
d'injection SQL, de forcer des chemins hors API, ni de toucher /api/replicate/*.

Ne cherche PAS a executer. Liste seulement les scenarios.

== FORMAT (markdown) ==

## Scenarios de test — {API_NAME}

### <METHODE chemin> — <categorie (ex: Isolation)>
| # | Scenario | Requete (methode/chemin/auth/corps) | Resultat attendu | Reference code |
|---|----------|--------------------------------------|------------------|----------------|
| 1 | ... | GET /documents/{{id}} avec token A sur doc de B | 403 | _check_ownership |

(repete par endpoint et par categorie)

### 🔍 Scenarios non evidents (deduits)
| # | Scenario | Pourquoi c'est un risque | Resultat attendu |
|---|----------|--------------------------|------------------|

### ⚠️ Points bloquants avant test
(pre-requis de donnees, ambiguites...)

**Total scenarios : X**

Ne cree aucun fichier. Retourne uniquement ce rapport markdown.
""".strip()


def generer_scenarios(pistes_cible=None, groupe=None, priority_only=False, max_scenarios=0):
    label = f"API '{API_NAME}'" + (f" — groupe '{groupe}'" if groupe else "")
    print(f"\n[e2e_api] ═══ PASSE 2a (scenarios) — {label} ═══\n")
    contrat, reponses = charger_contrat()
    if contrat is None:
        return
    print(f"[e2e_api] Contrat charge : {len(contrat.get('endpoints', []))} endpoint(s)")
    if pistes_cible:
        print(f"[e2e_api] 🎯 Pistes ciblees : {', '.join(pistes_cible)}")
    if priority_only:
        print("[e2e_api] 🎯 Scenarios prioritaires uniquement.")

    prompt = construire_prompt_scenarios(contrat, reponses, pistes_cible=pistes_cible,
                                         priority_only=priority_only, max_scenarios=max_scenarios)
    reponse = appeler_claude_avec_retry(prompt)
    if not reponse:
        print("[e2e_api] ❌ Pas de reponse exploitable.")
        return

    os.makedirs(CONTRATS_DIR, exist_ok=True)
    path = os.path.join(CONTRATS_DIR, _nom_fichier("scenarios.md", groupe=groupe))
    with open(path, "w", encoding="utf-8") as f:
        f.write(reponse)
    print(f"[e2e_api] ✅ Scenarios sauvegardes : {path}")
    print("\n" + "=" * 70)
    print(reponse)
    print("=" * 70)


# ══════════════════════════════════════════════════════════════════════════════
# PROMPT PASSE 2b — EXECUTION (Playwright request, HTTP)
# ══════════════════════════════════════════════════════════════════════════════

def _bloc_env_execution():
    return f"""== ENVIRONNEMENT DE TEST ==
- URL de base de l'API : {BASE_URL}
- Ville de test par defaut : {VILLE}
- Tokens (Authorization: Bearer <token>) :
    * Projet A  : {TOKENS['A']}
    * Projet B  : {TOKENS['B']}
    * Admin     : {TOKENS['ADMIN']}
    * Legacy    : {TOKENS['LEGACY']}
  (Ce sont des tokens de TEST d'une instance jetable, prevus pour ca.)
- Endpoint de sante : GET {BASE_URL}/health (doit repondre 200 avant de tester)."""


def construire_prompt_run(contrat, scenarios, reponses, priority_only=False,
                           max_scenarios=0, groupe=None):
    contrat_json = json.dumps(contrat, indent=2, ensure_ascii=False)
    bloc_reponses = f"\n== CONTEXTE METIER ==\n{reponses}\n" if reponses else ""

    return f"""Tu vas tester l'API HTTP "{API_NAME}" (aucune interface : tout est HTTP).
Tu ecris et executes des tests Playwright en mode `request` (HTTP pur, PAS de
navigateur). Utilise l'API `request` de Playwright (apiRequest / request context)
pour envoyer les requetes et verifier statuts + corps JSON.

{_bloc_env_execution()}
{bloc_reponses}
== REFERENTIEL (Contrat) ==
{contrat_json}

== PLAN DE SCENARIOS A EXECUTER ==
{scenarios}

== COMMENT PROCEDER ==
1. Ecris un fichier de specs Playwright `request` (JS) dans "tests/{API_NAME}.spec.js"
   couvrant les scenarios ci-dessus. Utilise le request context avec baseURL
   = {BASE_URL}. Mets les tokens en tete de fichier (ce sont des tokens de test).
2. Verifie d'abord que GET /health repond 200 (sinon attends quelques secondes).
3. Pour l'isolation : uploade un doc avec le token du projet B, recupere son
   document_id / archive_path, puis tente d'y acceder avec le token A -> l'attendu
   est 403. Distingue bien 403 (interdit) de 404 (fuite d'existence) et 200 (bypass).
4. Execute les specs avec `npx playwright test tests/{API_NAME}.spec.js`.
5. NE TESTE PAS les endpoints /api/replicate/* (hors perimetre).

== REGLE ANTI-ACHARNEMENT ==
- Une lenteur reseau n'est PAS un echec : attends que l'API reponde.
- Une assertion qui echoue vraiment : marque le scenario FAIL avec le statut
  reel obtenu vs attendu, et passe au suivant. Pas d'acharnement.
- Un ecart de comportement (ex: 404 la ou on attend 403, ou 200 la ou on attend
  403) est un FAIL a documenter precisement : c'est exactement ce qu'on cherche.

== LIVRABLE ==
Redige un "Rapport E2E" final en Markdown avec OBLIGATOIREMENT deux tableaux :

### 1. Résumé d'exécution
| Catégorie | Réussis ✅ | Anomalies ❌ | Total |
| :--- | :---: | :---: | :---: |
| Isolation | 4 | 1 | 5 |
| Auth | 5 | 0 | 5 |

### 2. Détail des Anomalies (Focus Scénarios)
| Nom du Test | Scénario Exact (Étapes) | Cause de l'Erreur |
| :--- | :--- | :--- |
| **Accès croisé A→B (3)** | 1. Upload doc avec token B<br>2. GET /documents/{{id}} avec token A | Renvoie 404 au lieu de 403 : fuite d'existence |

Dans 'Nom du Test', inclus TOUJOURS le numero du scenario entre parentheses.
Ne liste dans le 2e tableau QUE les FAIL.
Sous le dernier tableau, ajoute : **Total : X/Y PASS**
"""


def construire_prompt_run_live(contrat, scenarios, reponses, priority_only=False,
                                max_scenarios=0, groupe=None):
    base = construire_prompt_run(contrat, scenarios, reponses, priority_only=priority_only,
                                 max_scenarios=max_scenarios, groupe=groupe).split("== LIVRABLE ==", 1)[0].rstrip()
    rapport_live_path = f"reports/{_nom_fichier('rapport_live.md', groupe=groupe)}"
    bloc_live = f"""
== LIVRABLE — RAPPORT ECRIT EN DIRECT (OBLIGATOIRE, AU FUR ET A MESURE) ==
Tu ecris le rapport progressivement dans "{rapport_live_path}", des le premier
scenario, pour qu'une coupure ne perde jamais le travail deja fait.

1. AVANT de tester, cree "{rapport_live_path}" avec ce squelette EXACT :

# Rapport E2E — {API_NAME}

## Detail des scenarios

## Detail des Anomalies (Focus Scenarios)
| Nom du Test | Scenario Exact (Etapes) | Cause de l'Erreur |
| :--- | :--- | :--- |

2. Apres CHAQUE scenario execute (jamais en lot) :
   a) Trouve/cree la sous-section de sa CATEGORIE (exactement celle du plan) :
      "### <METHODE chemin> — <categorie>" puis l'en-tete :
      | # | Scenario | Resultat | Constat |
      |---|----------|----------|---------|
      Puis AJOUTE : | <numero du plan> | <nom> | PASS/FAIL/BLOQUE | <statut reel vs attendu> |
   b) UNIQUEMENT si FAIL, ajoute aussi une ligne au tableau Anomalies :
      | **<nom> (<numero>)** | 1. <etape><br>2. <etape>... | <cause: statut obtenu vs attendu> |

3. Un constat reflete TOUJOURS une observation reelle (statut/corps recu), jamais une supposition.

== A LA TOUTE FIN SEULEMENT ==
Ajoute le tableau recapitulatif :

### Resume d'execution
| Catégorie | Réussis ✅ | Anomalies ❌ | Total |
| :--- | :---: | :---: | :---: |
| Isolation | 4 | 1 | 5 |

Sous ce tableau : **Total : X/Y PASS**
Puis reponds UNIQUEMENT "OK - rapport ecrit dans {rapport_live_path}". N'affiche pas le rapport.
"""
    return base + "\n\n" + bloc_live.strip() + "\n"


# ══════════════════════════════════════════════════════════════════════════════
# GUARD ANTI-REFUS + POST GITHUB  (repris de e2e_v2)
# ══════════════════════════════════════════════════════════════════════════════

def _ressemble_a_un_refus(texte):
    if not texte:
        return False
    marqueurs = [
        "i'm not going to", "i won't", "i am not going to",
        "je ne vais pas", "je refuse", "i'm going to stop here",
        "before doing anything else", "social-engineering", "prompt injection",
    ]
    return any(m in texte[:800].lower() for m in marqueurs)


def appeler_claude_execution_avec_retry(prompt, timeout=3600, max_tentatives=3, cwd=None):
    for tentative in range(1, max_tentatives + 1):
        reponse = appeler_claude_avec_retry(prompt, timeout=timeout, cwd=cwd)
        if not reponse:
            return reponse
        if not _ressemble_a_un_refus(reponse):
            return reponse
        print(f"[e2e_api] ℹ️  Tentative {tentative}/{max_tentatives} : reponse type pause de securite.")
        if tentative < max_tentatives:
            print("[e2e_api] 🔁 Nouvel appel independant (nouveau contexte)...")
    print("[e2e_api] ⚠️  Toutes les tentatives etaient des pauses. Verifie la reponse.")
    return reponse


def poster_rapport_github(rapport: str):
    import urllib.request
    token = os.environ.get('GITHUB_TOKEN')
    if not token:
        env_path = os.path.join(ROOT, '.env')
        if os.path.exists(env_path):
            with open(env_path, encoding='utf-8') as f:
                for line in f:
                    if line.startswith('GITHUB_TOKEN='):
                        token = line.strip().split('=', 1)[1]
                        break
    if not token:
        print("[e2e_api] ⚠️  GITHUB_TOKEN manquant — rapport non poste (sauvegarde locale seulement)")
        return False

    url = f"https://api.github.com/repos/{GITHUB_REPO}/issues/{GITHUB_ISSUE_NUMBER}/comments"
    payload = json.dumps({"body": rapport}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST", headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status == 201:
                print(f"[e2e_api] ✅ Rapport poste dans Issue #{GITHUB_ISSUE_NUMBER}")
                print(f"[e2e_api] 🔗 https://github.com/{GITHUB_REPO}/issues/{GITHUB_ISSUE_NUMBER}")
                return True
            print(f"[e2e_api] ❌ GitHub API : statut {resp.status}")
            return False
    except Exception as e:
        print(f"[e2e_api] ❌ Erreur reseau GitHub : {e}")
        return False


# ══════════════════════════════════════════════════════════════════════════════
# TRONCATURE REELLE DES SCENARIOS  (repris de e2e_v2)
# ══════════════════════════════════════════════════════════════════════════════

def _compter_scenarios_section(lignes):
    n = 0
    for l in lignes:
        s = l.strip()
        if s.startswith('|'):
            c0 = s.strip('|').split('|')[0].strip()
            if c0.isdigit():
                n += 1
    return n


def _tronquer_scenarios(md, max_scenarios):
    if not max_scenarios or max_scenarios <= 0:
        return md, False
    lignes = md.split('\n')
    sections, courante = [], []
    for l in lignes:
        if l.startswith('### '):
            if courante:
                sections.append(courante)
            courante = [l]
        else:
            courante.append(l)
    if courante:
        sections.append(courante)
    if not sections:
        return md, False
    gardees, total, tronque = [], 0, False
    for s in sections:
        taille = _compter_scenarios_section(s)
        if taille == 0:
            gardees.append(s)
            continue
        if total >= max_scenarios:
            tronque = True
            continue
        gardees.append(s)
        total += taille
    return '\n'.join('\n'.join(s) for s in gardees), tronque


# ══════════════════════════════════════════════════════════════════════════════
# PASSE 2b — EXECUTION
# ══════════════════════════════════════════════════════════════════════════════

def executer_tests(priority_only=False, max_scenarios=0, groupe=None):
    label = f"API '{API_NAME}'" + (f" — groupe '{groupe}'" if groupe else "")
    print(f"\n[e2e_api] ═══ PASSE 2b (execution HTTP) — {label} ═══\n")

    contrat, reponses = charger_contrat()
    if contrat is None:
        return

    scenarios_path = os.path.join(CONTRATS_DIR, _nom_fichier("scenarios.md", groupe=groupe))
    if not os.path.exists(scenarios_path):
        print(f"[e2e_api] ❌ Scenarios introuvables : {scenarios_path}")
        suff = f" --groupe={groupe}" if groupe else ""
        print(f"[e2e_api] Lance d'abord : python e2e_api.py --generate-scenarios{suff}")
        return
    with open(scenarios_path, encoding='utf-8') as f:
        scenarios = f.read()

    if max_scenarios:
        print(f"[e2e_api] 🔒 Plafond : {max_scenarios} scenarios.")
        scenarios, tronque = _tronquer_scenarios(scenarios, max_scenarios)
        if tronque:
            print(f"[e2e_api] ✂️  Scenarios tronques a {max_scenarios} (reste non envoye).")

    print(f"[e2e_api] ⚠️  L'execution va REELLEMENT taper {BASE_URL}")

    if RAPPORT_LIVE:
        print("[e2e_api] 📝 Mode RAPPORT LIVE actif.")
        prompt = construire_prompt_run_live(contrat, scenarios, reponses,
                                            priority_only=priority_only,
                                            max_scenarios=max_scenarios, groupe=groupe)
        rapport_live_path = os.path.join(REPORTS_DIR, _nom_fichier("rapport_live.md", groupe=groupe))
        os.makedirs(REPORTS_DIR, exist_ok=True)
        if os.path.exists(rapport_live_path):
            os.remove(rapport_live_path)
    else:
        prompt = construire_prompt_run(contrat, scenarios, reponses,
                                       priority_only=priority_only,
                                       max_scenarios=max_scenarios, groupe=groupe)
        rapport_live_path = None

    reponse = appeler_claude_execution_avec_retry(prompt, timeout=3600)

    rapport_partiel = False
    if RAPPORT_LIVE and rapport_live_path and os.path.exists(rapport_live_path):
        with open(rapport_live_path, encoding="utf-8") as f:
            contenu = f.read().strip()
        if not reponse:
            print("[e2e_api] ⚠️  Session coupee — rapport live recupere.")
            rapport_partiel = True
        else:
            print("[e2e_api] ✅ Rapport live complet.")
        reponse = contenu
    elif not reponse:
        print("[e2e_api] ❌ Pas de rapport exploitable.")
        return

    os.makedirs(REPORTS_DIR, exist_ok=True)
    now = datetime.now().strftime('%Y%m%d_%H%M%S')
    rapport_path = os.path.join(REPORTS_DIR, _nom_fichier(f"rapport_{now}.md", groupe=groupe))
    with open(rapport_path, "w", encoding="utf-8") as f:
        f.write(reponse)
    print(f"[e2e_api] ✅ Rapport sauvegarde : {rapport_path}")

    prefixe = "⚠️ [PARTIEL] " if rapport_partiel else ""
    en_tete = f"## 🧪 {prefixe}Rapport E2E API — {API_NAME} — {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
    poster_rapport_github(en_tete + reponse)

    print("\n" + "=" * 70)
    print(reponse)
    print("=" * 70)


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    analyze = gen = run = reuse = priority_only = False
    max_scenarios = 0
    pistes_cible = None
    groupe = None

    for arg in sys.argv[1:]:
        if arg == '--analyze':
            analyze = True
        elif arg == '--generate-scenarios':
            gen = True
        elif arg == '--run':
            run = True
        elif arg == '--reuse-if-exists':
            reuse = True
        elif arg == '--priority-only':
            priority_only = True
        elif arg.startswith('--max-scenarios='):
            try:
                max_scenarios = int(arg.split('=', 1)[1].strip())
            except ValueError:
                max_scenarios = 0
        elif arg.startswith('--pistes='):
            v = arg.split('=', 1)[1].strip()
            pistes_cible = [p.strip() for p in v.split(',') if p.strip()] or None
        elif arg.startswith('--groupe='):
            groupe = arg.split('=', 1)[1].strip() or None

    if not (analyze or gen or run):
        print("""
e2e_api.py — E2E pour une API HTTP (doc-archiver)

  # Passe 1 : analyser le code -> Contrat JSON (endpoints) + resume + pistes
  python e2e_api.py --analyze
  python e2e_api.py --analyze --reuse-if-exists

  # (entre les deux : choisir les pistes via form_server.py + ngrok)

  # Passe 2a : generer le plan de scenarios des pistes choisies
  python e2e_api.py --generate-scenarios --pistes=isolation,auth

  # Passe 2b : ecrire + executer les specs Playwright request, rapport en issue
  python e2e_api.py --run --pistes=isolation --max-scenarios=30

Options : --priority-only  --max-scenarios=N  --pistes=id1,id2  --groupe=NOM

Env clefs : API_NAME, SOURCE_DIR (repo doc-archiver), BASE_URL, VILLE,
            TOKEN_A/TOKEN_B/ADMIN_TOKEN/LEGACY_TOKEN, RAPPORT_LIVE,
            GITHUB_REPO, GITHUB_ISSUE_NUMBER, GITHUB_TOKEN.
""".strip())
        sys.exit(0)

    if analyze:
        analyser_api(force=not reuse)
    if gen:
        generer_scenarios(pistes_cible=pistes_cible, groupe=groupe,
                          priority_only=priority_only, max_scenarios=max_scenarios)
    if run:
        executer_tests(priority_only=priority_only, max_scenarios=max_scenarios, groupe=groupe)


if __name__ == '__main__':
    main()
