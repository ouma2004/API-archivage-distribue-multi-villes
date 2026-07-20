# replicator.py — Service de réplication inter-sites (asynchrone, via VPN HTTP)

import asyncio
import io
import logging

import httpx

from config import (
    is_single_server, get_backup_ville, get_site_vpn_url_safe,
    MINIO_ACCESS_KEY, MINIO_SECRET_KEY,
    REPLICATION_STATUS_SYNCING, REPLICATION_STATUS_SYNCED, REPLICATION_STATUS_FAILED,
)
from database import update_replication_status, get_pending_replications
from archiver import get_file_stream

logger = logging.getLogger("replicator")

REPLICATE_ENDPOINT_TEMPLATE = "/api/replicate/{ville}"
DELETE_ENDPOINT_TEMPLATE = "/api/replicate/{ville}/by-path"
REQUEST_TIMEOUT_S = 600


async def replicate_document_async(
    ville: str, id_doc: int, filename: str, archive_path: str,
    file_type: str, nb_pages: int = 1, backup_ville_override: str | None = None,
    project_bucket: str | None = None,
):
    """
    Réplique UN document vers la ville de secours, en tâche de fond.

    project_bucket : si le document appartient à un projet (bucket dédié),
    ce paramètre permet de lire la SOURCE dans le bon bucket plutôt que
    dans le bucket "documents" par défaut (corrige l'erreur NoSuchKey
    observée pour les documents multi-projets).

    La lecture se fait avec les identifiants ROOT MinIO (MINIO_ACCESS_KEY/
    MINIO_SECRET_KEY) — légitime ici : la réplication est une opération
    système interne, pas une action au nom d'un utilisateur/projet précis.
    """
    if not is_single_server(ville) and not backup_ville_override:
        logger.info(f"[REPLICATOR] {ville} est CLUSTER — réplication non requise (doc {id_doc})")
        return

    backup_ville = backup_ville_override or get_backup_ville(ville)
    if not backup_ville:
        logger.warning(f"[REPLICATOR] {ville} sans backup_of configuré et aucun override fourni")
        update_replication_status(ville, id_doc, REPLICATION_STATUS_FAILED)
        return

    update_replication_status(ville, id_doc, REPLICATION_STATUS_SYNCING)
    logger.info(f"[REPLICATOR] Début réplication doc {id_doc} : {ville} → {backup_ville}")

    try:
        # ── 1. Récupère le fichier depuis MinIO local (bon bucket) ──────
        source_project = (
            {"bucket": project_bucket, "access_key": MINIO_ACCESS_KEY, "secret_key": MINIO_SECRET_KEY}
            if project_bucket else None
        )
        stream, content_type = get_file_stream(ville, archive_path, project=source_project)
        file_bytes = stream.read()

        # ── 2. Envoie vers l'API de la ville de secours via VPN ────────
        target_url = get_site_vpn_url_safe(backup_ville) + \
                     REPLICATE_ENDPOINT_TEMPLATE.format(ville=backup_ville)

        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S) as client:
            response = await client.post(
                target_url,
                files={"file": (filename, file_bytes, content_type)},
                data={
                    "source_ville": ville, "source_id_doc": str(id_doc),
                    "file_type": file_type, "nb_pages": str(nb_pages),
                },
            )
            response.raise_for_status()
            result = response.json()

        backup_archive_path = result.get("archive_path")

        # ── 3. Succès — met à jour le statut local ──────────────────────
        update_replication_status(
            ville, id_doc, REPLICATION_STATUS_SYNCED,
            backup_ville=backup_ville, backup_archive_path=backup_archive_path,
        )
        logger.info(f"[REPLICATOR] ✓ Doc {id_doc} répliqué vers {backup_ville} (clé={backup_archive_path})")

    except Exception as e:
        import traceback
        logger.error(f"[REPLICATOR] ✗ Échec réplication doc {id_doc} ({ville}→{backup_ville}) : {e}")
        logger.error(f"[REPLICATOR] Traceback complet:\n{traceback.format_exc()}")
        update_replication_status(ville, id_doc, REPLICATION_STATUS_FAILED)


def schedule_replication(
    ville: str, id_doc: int, filename: str, archive_path: str,
    file_type: str, nb_pages: int = 1, backup_ville_override: str | None = None,
    project_bucket: str | None = None,
):
    """Lance la réplication en arrière-plan SANS bloquer l'appelant."""
    asyncio.create_task(
        replicate_document_async(
            ville, id_doc, filename, archive_path,
            file_type, nb_pages, backup_ville_override, project_bucket,
        )
    )


# ═══════════════════════════════════════════════════════════════════════════
# Suppression propagée vers la copie de secours
# ═══════════════════════════════════════════════════════════════════════════

async def delete_backup_async(backup_ville: str, backup_archive_path: str, hard: bool):
    target_url = get_site_vpn_url_safe(backup_ville) + \
                 DELETE_ENDPOINT_TEMPLATE.format(ville=backup_ville)
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S) as client:
            response = await client.delete(
                target_url,
                params={"archive_path": backup_archive_path, "hard": hard},
            )
            response.raise_for_status()
        logger.info(f"[REPLICATOR] ✓ Suppression propagée vers {backup_ville} ({backup_archive_path})")
    except Exception as e:
        logger.error(f"[REPLICATOR] ✗ Échec propagation suppression vers {backup_ville} : {e}")


def schedule_backup_deletion(backup_ville: str, backup_archive_path: str, hard: bool):
    asyncio.create_task(delete_backup_async(backup_ville, backup_archive_path, hard))


# ═══════════════════════════════════════════════════════════════════════════
# Worker périodique — retente les réplications PENDING/FAILED
# ═══════════════════════════════════════════════════════════════════════════

async def retry_pending_replications(ville: str):
    """
    Parcourt les documents PENDING/FAILED de la ville et relance leur
    réplication — y compris ceux liés à un projet (project_bucket transmis).
    """
    pending = get_pending_replications(ville)
    if not pending:
        return

    logger.info(f"[REPLICATOR] {len(pending)} document(s) en attente pour '{ville}'")

    for doc in pending:
        await replicate_document_async(
            ville, doc["id_doc"], doc["filename"], doc["archive_path"],
            doc["file_type"], doc["nb_pages"],
            project_bucket=doc.get("project_bucket"),
        )


async def replication_background_loop(villes: list[str], interval_s: int = 60):
    """Boucle de fond — retente périodiquement les réplications en échec/attente."""
    while True:
        for ville in villes:
            try:
                await retry_pending_replications(ville)
            except Exception as e:
                logger.error(f"[REPLICATOR] Erreur boucle fond ({ville}) : {e}")
        await asyncio.sleep(interval_s)