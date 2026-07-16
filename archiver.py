# archiver.py — Stockage MinIO multi-villes
# Structure clé : [<folder_prefix>/]YYYY/MM/DD/uuid_filename

import uuid
from datetime import datetime
from pathlib import Path

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

from config import VILLES, validate_ville


def _get_client(ville: str, public: bool = False, project: dict | None = None):
    validate_ville(ville)
    cfg = VILLES[ville]["minio"]
    endpoint = cfg["public_endpoint"] if public else cfg["endpoint"]

    access_key = project["access_key"] if project else cfg["access_key"]
    secret_key = project["secret_key"] if project else cfg["secret_key"]

    return boto3.client(
        "s3",
        endpoint_url      = f"{'https' if cfg['secure'] else 'http'}://{endpoint}",
        aws_access_key_id = access_key,
        aws_secret_access_key = secret_key,
        config            = Config(signature_version="s3v4"),
        region_name       = "us-east-1",
    )


def _resolve_bucket(ville: str, project: dict | None = None) -> str:
    if project:
        return project["bucket"]
    return VILLES[ville]["minio"]["bucket"]


def ensure_bucket(ville: str, max_retries: int = 12, delay_s: float = 5.0):
    import time

    validate_ville(ville)
    bucket = VILLES[ville]["minio"]["bucket"]
    client = _get_client(ville)

    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            client.head_bucket(Bucket=bucket)
            return
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code == "404" or "NoSuchBucket" in str(e):
                try:
                    client.create_bucket(Bucket=bucket)
                    print(f"  [MINIO] Bucket '{bucket}' créé pour '{ville}'")
                    return
                except ClientError as create_err:
                    last_error = create_err
            else:
                last_error = e
            print(f"  [MINIO] '{ville}' pas encore prêt (tentative {attempt}/{max_retries}) — {last_error}")
            time.sleep(delay_s)

    raise RuntimeError(f"MinIO '{ville}' indisponible après {max_retries} tentatives : {last_error}")


def ensure_all_buckets():
    for ville in VILLES:
        ensure_bucket(ville)


def archive_file(ville: str, src_path: Path, original_name: str,
                  folder_prefix: str | None = None,
                  project: dict | None = None) -> str:
    validate_ville(ville)
    bucket = _resolve_bucket(ville, project)

    now       = datetime.now()
    uid       = uuid.uuid4().hex[:8]
    safe_name = original_name.replace(" ", "_")
    date_part = now.strftime("%Y/%m/%d")

    key = f"{folder_prefix}/{date_part}/{uid}_{safe_name}" if folder_prefix \
        else f"{date_part}/{uid}_{safe_name}"

    client = _get_client(ville, project=project)
    client.upload_file(
        str(src_path), bucket, key,
        ExtraArgs={"ContentType": _content_type(original_name)},
    )

    print(f"  [MINIO:{ville}] Uploadé → {bucket}/{key}")
    return key


def get_file_url(ville: str, key: str, expires_in: int = 3600,
                  project: dict | None = None) -> str:
    validate_ville(ville)
    bucket = _resolve_bucket(ville, project)
    client = _get_client(ville, public=True, project=project)
    return client.generate_presigned_url(
        "get_object",
        Params    = {"Bucket": bucket, "Key": key},
        ExpiresIn = expires_in,
    )


def get_file_stream(ville: str, key: str, project: dict | None = None):
    from config import VILLES_VALIDES, get_remote_api_url, API_TOKEN

    if ville not in VILLES_VALIDES:
        remote_url = get_remote_api_url(ville)
        if not remote_url:
            raise Exception(f"Ville '{ville}' inconnue et aucune URL distante configurée")

        import httpx, io
        resp = httpx.get(
            f"{remote_url}/file",
            params={"ville": ville, "archive_path": key},
            headers={"Authorization": f"Bearer {API_TOKEN}"},
            timeout=30,
        )
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "application/octet-stream")
        return io.BytesIO(resp.content), content_type

    validate_ville(ville)
    bucket   = _resolve_bucket(ville, project)
    endpoint = VILLES[ville]["minio"]["endpoint"]
    public   = ".svc.cluster.local" not in endpoint
    client   = _get_client(ville, public=public, project=project)

    response     = client.get_object(Bucket=bucket, Key=key)
    content_type = response.get("ContentType", "application/octet-stream")
    return response["Body"], content_type


def delete_file(ville: str, key: str, project: dict | None = None) -> None:
    validate_ville(ville)
    bucket = _resolve_bucket(ville, project)
    client = _get_client(ville, project=project)
    client.delete_object(Bucket=bucket, Key=key)
    print(f"  [MINIO:{ville}] Supprimé définitivement → {bucket}/{key}")


def _content_type(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    return {
        ".pdf": "application/pdf", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".png": "image/png", ".tiff": "image/tiff", ".tif": "image/tiff",
        ".webp": "image/webp",
        ".zip": "application/zip",
        ".mp4": "video/mp4", ".mov": "video/quicktime",
        ".avi": "video/x-msvideo", ".mkv": "video/x-matroska",
        ".bak": "application/octet-stream",
    }.get(ext, "application/octet-stream")