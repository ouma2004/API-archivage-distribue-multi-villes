# config.py — Configuration multi-villes + réplication inter-sites + projets

import json
import os
from pathlib import Path

API_TOKEN = os.getenv("API_TOKEN", "dev-secret-token")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "").strip() or None
MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "50"))
TEMP_DIR = Path(os.getenv("TEMP_DIR", "/tmp/api_archiver"))
TEMP_DIR.mkdir(parents=True, exist_ok=True)

POSTGRES_USER = os.getenv("POSTGRES_USER", "postgres")
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD", "postgres")
POSTGRES_DATABASE = os.getenv("POSTGRES_DATABASE", "archive")

MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY", "minioadmin")
MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY", "minioadmin")
MINIO_BUCKET = os.getenv("MINIO_BUCKET", "documents")

VILLES_ACTIVES = [
    v.strip() for v in os.getenv("VILLES_ACTIVES", "").split(",")
    if v.strip()
]

def _build_villes_config() -> dict:
    villes = {}
    for ville in VILLES_ACTIVES:
        prefix = ville.upper()
        postgres_host = os.getenv(f"{prefix}_POSTGRES_HOST", f"postgres-{ville}")
        minio_endpoint = os.getenv(f"{prefix}_MINIO_ENDPOINT", f"minio-{ville}-1:9000")
        minio_public = os.getenv(f"{prefix}_MINIO_PUBLIC", "").strip() or minio_endpoint

        villes[ville] = {
            "postgres": {
                "host": postgres_host, "port": 5432, "database": POSTGRES_DATABASE,
                "user": POSTGRES_USER, "password": POSTGRES_PASSWORD,
            },
            "minio": {
                "endpoint": minio_endpoint, "public_endpoint": minio_public,
                "access_key": MINIO_ACCESS_KEY, "secret_key": MINIO_SECRET_KEY,
                "bucket": MINIO_BUCKET, "secure": False,
            },
        }
    return villes

VILLES = _build_villes_config()
VILLES_VALIDES = set(VILLES.keys())

def validate_ville(ville: str) -> None:
    if ville not in VILLES_VALIDES:
        raise ValueError(
            f"Ville inconnue : '{ville}'. Villes valides : {', '.join(sorted(VILLES_VALIDES))}"
        )

def validate_ville_or_remote(ville: str) -> None:
    if ville in VILLES_VALIDES:
        return
    if ville in REMOTE_API_URLS:
        return
    raise ValueError(
        f"Ville inconnue (ni locale ni distante) : '{ville}'. "
        f"Villes locales : {', '.join(sorted(VILLES_VALIDES))}. "
        f"Villes distantes : {', '.join(sorted(REMOTE_API_URLS.keys()))}"
    )

# ═══════════════════════════════════════════════════════════════════════════
# PROJETS (multi-tenant) — un token = un projet = un bucket + compte MinIO
# PROJECTS_JSON = {"token_x":{"name":"ProjetA","bucket":"documents-projeta","access_key":"...","secret_key":"..."}}
# ═══════════════════════════════════════════════════════════════════════════
PROJECTS: dict[str, dict] = {}
_projects_raw = os.getenv("PROJECTS_JSON", "").strip()
if _projects_raw:
    try:
        PROJECTS = json.loads(_projects_raw)
    except Exception as e:
        print(f" [CONFIG] Erreur parsing PROJECTS_JSON : {e}")

def get_project_for_token(token: str) -> dict | None:
    return PROJECTS.get(token)

# ═══════════════════════════════════════════════════════════════════════════
# RÉPLICATION INTER-SITES
# ═══════════════════════════════════════════════════════════════════════════
SITE_TYPE_SINGLE = "SINGLE_SERVER"
SITE_TYPE_CLUSTER = "CLUSTER"

REPLICATION_STATUS_PENDING = "PENDING"
REPLICATION_STATUS_SYNCING = "SYNCING"
REPLICATION_STATUS_SYNCED = "SYNCED"
REPLICATION_STATUS_FAILED = "FAILED"

REPLICATION_POLICY_LOCAL_ONLY = "LOCAL_ONLY"
REPLICATION_POLICY_BACKUP = "BACKUP"
REPLICATION_POLICY_FULL = "FULL"

_API_SELF_HOST = os.getenv("API_SELF_HOST", "api")
_API_SELF_PORT = int(os.getenv("API_SELF_PORT", "8000"))

def _build_sites_registry() -> dict:
    registry = {}
    for ville in VILLES_ACTIVES:
        prefix = ville.upper()
        site_type = os.getenv(f"{prefix}_SITE_TYPE", SITE_TYPE_CLUSTER)
        backup_of = os.getenv(f"{prefix}_BACKUP_OF", "").strip() or None
        site_id = int(os.getenv(f"{prefix}_SITE_ID", str(VILLES_ACTIVES.index(ville) + 1)))
        registry[ville] = {
            "site_id": site_id, "site_name": ville.capitalize(), "site_type": site_type,
            "vpn_ip": _API_SELF_HOST, "api_port": _API_SELF_PORT, "backup_of": backup_of,
        }
    return registry

SITES_REGISTRY = _build_sites_registry()

def is_single_server(ville: str) -> bool:
    validate_ville(ville)
    return SITES_REGISTRY[ville]["site_type"] == SITE_TYPE_SINGLE

def get_backup_ville(ville: str) -> str | None:
    validate_ville(ville)
    return SITES_REGISTRY[ville].get("backup_of")

def get_site_vpn_url(ville: str) -> str:
    validate_ville(ville)
    site = SITES_REGISTRY[ville]
    return f"http://{site['vpn_ip']}:{site['api_port']}"

def get_replication_policy(ville: str) -> str:
    if is_single_server(ville) and get_backup_ville(ville):
        return REPLICATION_POLICY_BACKUP
    return REPLICATION_POLICY_LOCAL_ONLY

# ═══════════════════════════════════════════════════════════════════════════
# URLs des APIs distantes — pour réplication cross-site (multi-machines)
# ═══════════════════════════════════════════════════════════════════════════
REMOTE_API_URLS: dict[str, str] = {}
for _ville in ["fes", "casa", "rabat"]:
    _url = os.getenv(f"REMOTE_API_{_ville.upper()}", "").strip()
    if _url:
        REMOTE_API_URLS[_ville] = _url

def get_remote_api_url(ville: str) -> str | None:
    return REMOTE_API_URLS.get(ville)

def get_site_vpn_url_safe(ville: str) -> str:
    remote = get_remote_api_url(ville)
    if remote:
        return remote
    if ville in SITES_REGISTRY:
        site = SITES_REGISTRY[ville]
        return f"http://{site['vpn_ip']}:{site['api_port']}"
    raise Exception(f"Aucune URL connue pour la ville '{ville}' (locale ou distante)")