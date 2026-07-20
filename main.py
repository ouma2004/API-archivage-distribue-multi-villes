# main.py — API d'archivage distribué multi-villes

import asyncio
import hashlib
import uuid
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel

from archiver import (
    archive_file, ensure_all_buckets, get_file_url, get_file_stream, delete_file,
)
from config import (
    API_TOKEN, ADMIN_TOKEN, MAX_FILE_SIZE_MB, TEMP_DIR, VILLES_VALIDES,
    validate_ville, get_project_for_token, MINIO_ACCESS_KEY, MINIO_SECRET_KEY,
)
from database import (
    init_all_db, save_document, get_documents, get_document_full,
    soft_delete_document, delete_document, rename_document,
    get_document_by_archive_path,
)
from replicator import (
    schedule_replication, replication_background_loop, schedule_backup_deletion,
)

app = FastAPI(
    title="Archivage Distribué Multi-Villes",
    description=(
        "Stockage pur de documents (PDF, images, ou tout autre type) → "
        "MinIO (par ville) + PostgreSQL (par ville) → réplication inter-sites.\n\n"
        f"**Villes disponibles** : {', '.join(sorted(VILLES_VALIDES))}\n\n"
        "**Auth** : `Authorization: Bearer <token>`"
    ),
    version="6.1.1",
)


@app.on_event("startup")
def startup():
    init_all_db()
    ensure_all_buckets()
    print(f"  [APP] Villes : {', '.join(sorted(VILLES_VALIDES))}")
    asyncio.create_task(replication_background_loop(sorted(VILLES_VALIDES), interval_s=60))


security = HTTPBearer()


def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """
    Retourne :
    - {"is_admin": True} si token admin
    - dict projet si token projet connu
    - None si ancien API_TOKEN global (mode legacy)
    - lève 401 sinon
    """
    token = credentials.credentials

    if ADMIN_TOKEN and token == ADMIN_TOKEN:
        return {"is_admin": True}

    project = get_project_for_token(token)
    if project:
        return project

    if token == API_TOKEN:
        return None

    raise HTTPException(status_code=401, detail="Token invalide")


def _bucket_filter(identity) -> str | None:
    if identity is None:
        return None
    if identity.get("is_admin"):
        return "ALL"
    return identity.get("bucket")


def _check_ownership(identity, doc_info: dict):
    """403 si le document n'appartient pas au projet de l'appelant."""
    if identity and identity.get("is_admin"):
        return
    doc_bucket = doc_info.get("project_bucket")
    caller_bucket = identity.get("bucket") if identity else None
    if doc_bucket != caller_bucket:
        raise HTTPException(status_code=403, detail="Ce document n'appartient pas à votre projet")


def _effective_project_for_file_access(identity, doc_info: dict) -> dict | None:
    """Pour lire/supprimer un fichier MinIO : un admin doit utiliser le
    compte MinIO root avec le bucket réel du document (pas ses propres
    identifiants, puisqu'un admin n'a pas de bucket personnel)."""
    if identity and identity.get("is_admin"):
        bucket = doc_info.get("project_bucket")
        if bucket:
            return {"bucket": bucket, "access_key": MINIO_ACCESS_KEY, "secret_key": MINIO_SECRET_KEY}
        return None
    return identity


def verify_ville(ville: str):
    try:
        validate_ville(ville)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return ville


class RenameRequest(BaseModel):
    filename: str


# ── Endpoints ─────────────────────────────────────────────────────────────
@app.get("/health", tags=["Système"])
def health():
    return {"status": "ok", "villes": sorted(VILLES_VALIDES), "version": "6.1.1"}


@app.get("/whoami", tags=["Système"])
def whoami(project=Depends(verify_token)):
    if project and project.get("is_admin"):
        return {"mode": "admin", "project_name": "Administrateur (tous projets)"}
    if project is None:
        return {"mode": "legacy", "project_name": None}
    return {"mode": "project", "project_name": project.get("name")}


async def _process_single_upload(ville: str, backup_ville: str | None,
                                  file: UploadFile, project) -> dict:
    content = await file.read()

    if len(content) == 0:
        raise HTTPException(status_code=400, detail=f"Fichier vide : {file.filename}")
    if len(content) > MAX_FILE_SIZE_MB * 1024 * 1024:
        raise HTTPException(status_code=400, detail=f"{file.filename} dépasse {MAX_FILE_SIZE_MB} Mo")

    checksum = hashlib.sha256(content).hexdigest()
    ext = Path(file.filename).suffix.lower() if file.filename else ""
    tmp_path = TEMP_DIR / f"{uuid.uuid4().hex}{ext}"

    try:
        tmp_path.write_bytes(content)

        minio_key = archive_file(ville, tmp_path, file.filename, project=project)

        # Bucket effectif : None pour legacy ET pour admin (ni l'un ni
        # l'autre n'a de bucket-projet personnel dédié).
        effective_bucket = project["bucket"] if project and not project.get("is_admin") else None

        id_doc = save_document(
            ville=ville, filename=file.filename, archive_path=minio_key,
            file_type=ext.lstrip(".") or "bin", nb_pages=1, checksum=checksum,
            project_bucket=effective_bucket,
        )

        schedule_replication(
            ville=ville, id_doc=id_doc, filename=file.filename,
            archive_path=minio_key, file_type=ext.lstrip(".") or "bin",
            backup_ville_override=backup_ville,
            project_bucket=effective_bucket,          # ← FIX : corrige le NoSuchKey
        )

        try:
            download_url = get_file_url(ville, minio_key, project=project)
        except Exception as e:
            print(f"  [WARN] URL présignée indisponible pour {minio_key} : {e}")
            download_url = None

        return {
            "document_id": id_doc, "filename": file.filename, "checksum": checksum,
            "storage_server": ville, "backup_server": backup_ville,
            "archive_state": "ARCHIVED", "archive_path": minio_key,
            "size_bytes": len(content), "content_type": file.content_type,
            "download_url": download_url,
        }
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


@app.post("/documents", tags=["Archives"])
async def upload_document(
    ville: str = Query(..., description="Ville cible : " + ", ".join(sorted(VILLES_VALIDES))),
    backup_ville: str | None = Query(None, description="Ville de secours (optionnel)"),
    files: list[UploadFile] = File(..., description="Un ou plusieurs fichiers"),
    project=Depends(verify_token),
):
    """
    Upload un ou plusieurs fichiers en un seul appel. Aucune restriction de
    type (PDF, image, zip, .bak, vidéo, ou tout autre). Chaque fichier est
    traité indépendamment : un échec sur l'un n'empêche pas les autres.
    """
    verify_ville(ville)
    if backup_ville:
        verify_ville(backup_ville)

    uploaded = []
    errors = []

    for file in files:
        try:
            result = await _process_single_upload(ville, backup_ville, file, project)
            uploaded.append(result)
        except HTTPException as e:
            errors.append({"filename": file.filename, "detail": e.detail})

    status_code = 200 if uploaded else 400
    return JSONResponse(status_code=status_code, content={"uploaded": uploaded, "errors": errors})


@app.get("/documents", tags=["Archives"])
def list_documents(
    ville: str = Query(...),
    file_type: str | None = Query(None),
    limit: int = 20,
    include_deleted: bool = Query(False),
    project=Depends(verify_token),
):
    verify_ville(ville)
    docs = get_documents(
        ville, limit=limit, file_type=file_type, include_deleted=include_deleted,
        project_bucket_filter=_bucket_filter(project),
    )
    for doc in docs:
        try:
            eff = project
            if project and project.get("is_admin"):
                eff = {"bucket": doc["project_bucket"], "access_key": MINIO_ACCESS_KEY, "secret_key": MINIO_SECRET_KEY} \
                    if doc["project_bucket"] else None
            doc["download_url"] = get_file_url(ville, doc["archive_path"], project=eff)
        except Exception:
            doc["download_url"] = None
    return docs


@app.get("/documents/{doc_id}", tags=["Archives"])
def get_document(
    doc_id: int,
    ville: str = Query(...),
    project=Depends(verify_token),
):
    verify_ville(ville)

    doc_info = get_document_full(ville, doc_id)
    if not doc_info:
        raise HTTPException(status_code=404, detail=f"Document {doc_id} non trouvé")

    _check_ownership(project, doc_info)

    if doc_info["archive_state"] == "DELETED":
        raise HTTPException(status_code=410, detail=f"Document {doc_id} a été supprimé")

    filename = doc_info["filename"]
    archive_path = doc_info["archive_path"]
    effective_project = _effective_project_for_file_access(project, doc_info)

    try:
        stream, content_type = get_file_stream(ville, archive_path, project=effective_project)
        return StreamingResponse(
            stream, media_type=content_type,
            headers={"Content-Disposition": f'inline; filename="{filename}"'},
        )
    except Exception as e:
        print(f"  [GET:{ville}] Échec lecture primaire doc {doc_id} : {e}")

    if doc_info["replication_status"] == "SYNCED" and doc_info["backup_ville"]:
        backup_ville = doc_info["backup_ville"]
        backup_path = doc_info["backup_archive_path"]
        print(f"  [GET:{ville}] Fallback vers backup '{backup_ville}' ({backup_path})")
        try:
            # NOTE : le fallback utilise le bucket par défaut du site de
            # secours (les copies de secours y atterrissent toujours dans
            # documents/backup_from_<ville>/, jamais dans un bucket-projet
            # équivalent — choix assumé, cf. discussion réplication).
            stream, content_type = get_file_stream(backup_ville, backup_path)
            return StreamingResponse(
                stream, media_type=content_type,
                headers={"Content-Disposition": f'inline; filename="{filename}"'},
            )
        except Exception as e:
            raise HTTPException(status_code=503, detail=f"Site principal et secours indisponibles : {e}")

    raise HTTPException(status_code=503, detail=f"Site '{ville}' indisponible et aucune copie de secours")


@app.patch("/documents/{doc_id}", tags=["Archives"])
def rename_document_endpoint(
    doc_id: int,
    body: RenameRequest,
    ville: str = Query(...),
    project=Depends(verify_token),
):
    verify_ville(ville)

    doc_info = get_document_full(ville, doc_id)
    if not doc_info:
        raise HTTPException(status_code=404, detail=f"Document {doc_id} introuvable ou supprimé")

    _check_ownership(project, doc_info)

    new_name = body.filename.strip()
    if not new_name:
        raise HTTPException(status_code=400, detail="Nom de fichier vide")

    renamed = rename_document(ville, doc_id, new_name)
    if not renamed:
        raise HTTPException(status_code=404, detail=f"Document {doc_id} introuvable ou supprimé")
    return {"document_id": doc_id, "filename": new_name}


@app.delete("/documents/{doc_id}", tags=["Archives"])
def delete_document_endpoint(
    doc_id: int,
    ville: str = Query(...),
    hard: bool = Query(False, description="True = suppression physique irréversible."),
    project=Depends(verify_token),
):
    verify_ville(ville)

    doc_info = get_document_full(ville, doc_id)
    if not doc_info:
        raise HTTPException(status_code=404, detail=f"Document {doc_id} introuvable")

    _check_ownership(project, doc_info)
    effective_project = _effective_project_for_file_access(project, doc_info)

    if hard:
        archive_path = delete_document(ville, doc_id)
        if archive_path:
            try:
                delete_file(ville, archive_path, project=effective_project)
            except Exception as e:
                print(f"  [WARN] Échec suppression MinIO {archive_path} : {e}")
    else:
        deleted = soft_delete_document(ville, doc_id)
        if not deleted:
            raise HTTPException(status_code=404, detail=f"Document {doc_id} introuvable ou déjà supprimé")

    if doc_info.get("replication_status") == "SYNCED" and doc_info.get("backup_ville"):
        schedule_backup_deletion(
            doc_info["backup_ville"], doc_info["backup_archive_path"], hard,
        )

    return {"document_id": doc_id, "archive_state": "HARD_DELETED" if hard else "DELETED"}


@app.get("/file", tags=["Archives"])
def get_file_by_path(
    ville: str = Query(...),
    archive_path: str = Query(...),
    project=Depends(verify_token),
):
    verify_ville(ville)
    try:
        stream, content_type = get_file_stream(ville, archive_path, project=project)
        filename = archive_path.split("/")[-1]
        return StreamingResponse(
            stream, media_type=content_type,
            headers={"Content-Disposition": f'inline; filename="{filename}"'},
        )
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@app.post("/api/replicate/{ville}", tags=["Réplication"])
async def receive_replication_for_ville(
    ville: str,
    file: UploadFile = File(...),
    source_ville: str = Form(...),
    source_id_doc: str = Form(...),
    file_type: str = Form(...),
    nb_pages: int = Form(1),
):
    verify_ville(ville)
    if not source_ville or not source_ville.strip():
        raise HTTPException(status_code=400, detail="source_ville manquant")

    from config import SITES_REGISTRY
    source_site_id = SITES_REGISTRY.get(source_ville, {}).get("site_id", 0)

    content = await file.read()
    tmp_path = TEMP_DIR / f"{uuid.uuid4().hex}_{Path(file.filename).suffix}"
    tmp_path.write_bytes(content)

    try:
        backup_key = archive_file(
            ville, tmp_path, file.filename, folder_prefix=f"backup_from_{source_ville}",
        )

        id_doc_backup = save_document(
            ville=ville, filename=file.filename, archive_path=backup_key,
            file_type=file_type, nb_pages=nb_pages,
            source_site_id=source_site_id, is_primary=False,
        )

        print(f"  [REPLICATE] Reçu depuis {source_ville} (doc {source_id_doc}, "
              f"{nb_pages} page(s)) → stocké dans {ville} sous {backup_key} "
              f"(source_site_id={source_site_id})")

        return JSONResponse(content={
            "status": "received", "ville": ville,
            "id_doc": id_doc_backup, "archive_path": backup_key,
        })
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


@app.delete("/api/replicate/{ville}/by-path", tags=["Réplication"])
def delete_replica_by_path(
    ville: str,
    archive_path: str = Query(...),
    hard: bool = Query(False),
):
    verify_ville(ville)

    doc = get_document_by_archive_path(ville, archive_path)
    if not doc:
        return {"status": "already_absent", "ville": ville, "archive_path": archive_path}

    if hard:
        try:
            delete_file(ville, archive_path)
        except Exception as e:
            print(f"  [WARN] Échec suppression MinIO backup {archive_path} : {e}")
        delete_document(ville, doc["id_doc"])
    else:
        soft_delete_document(ville, doc["id_doc"])

    return {"status": "deleted", "ville": ville, "archive_path": archive_path}