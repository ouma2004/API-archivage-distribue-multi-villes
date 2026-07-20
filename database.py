# database.py — Connexions PostgreSQL par ville (système d'archivage uniquement)

import time
import psycopg2

from config import VILLES, validate_ville


def get_conn(ville: str):
    validate_ville(ville)
    cfg = VILLES[ville]["postgres"]

    for _ in range(20):
        try:
            return psycopg2.connect(
                host=cfg["host"], port=cfg["port"], database=cfg["database"],
                user=cfg["user"], password=cfg["password"],
            )
        except Exception:
            time.sleep(2)

    raise Exception(f"PostgreSQL indisponible pour la ville '{ville}'")


def init_db_for_ville(ville: str):
    conn = get_conn(ville)
    conn.autocommit = True
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS documents (
            id_doc              SERIAL PRIMARY KEY,
            filename            TEXT        NOT NULL,
            archive_path        TEXT        NOT NULL UNIQUE,
            file_type           TEXT        NOT NULL DEFAULT 'pdf',
            nb_pages            INTEGER     NOT NULL DEFAULT 1,
            created_at          TIMESTAMP   NOT NULL DEFAULT CURRENT_TIMESTAMP,
            source_site_id      INTEGER,
            current_site_id     INTEGER,
            replication_status  TEXT        NOT NULL DEFAULT 'PENDING',
            replication_policy  TEXT        NOT NULL DEFAULT 'LOCAL_ONLY',
            is_primary          BOOLEAN     NOT NULL DEFAULT TRUE,
            backup_ville        TEXT,
            backup_archive_path TEXT,
            replicated_at       TIMESTAMP
        );
    """)
    cur.execute("ALTER TABLE documents ADD COLUMN IF NOT EXISTS checksum TEXT;")
    cur.execute("ALTER TABLE documents ADD COLUMN IF NOT EXISTS archive_state TEXT NOT NULL DEFAULT 'ARCHIVED';")
    cur.execute("ALTER TABLE documents ADD COLUMN IF NOT EXISTS project_bucket TEXT;")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_documents_created ON documents(created_at DESC);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_documents_replication_status ON documents(replication_status);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_documents_archive_state ON documents(archive_state);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_documents_project_bucket ON documents(project_bucket);")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS sites (
            site_id    INTEGER     PRIMARY KEY,
            site_name  TEXT        NOT NULL,
            site_type  TEXT        NOT NULL,
            vpn_ip     TEXT        NOT NULL,
            updated_at TIMESTAMP   NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
    """)

    cur.close()
    conn.close()
    print(f"  [DB] Tables initialisées pour la ville '{ville}'")


def init_all_db():
    if not VILLES:
        print("  [DB] ⚠️  Aucune ville active (VILLES_ACTIVES vide) — rien à initialiser")
        return

    for ville in VILLES:
        init_db_for_ville(ville)
        sync_sites_table(ville)


def sync_sites_table(ville: str):
    from config import SITES_REGISTRY
    conn = get_conn(ville)
    conn.autocommit = True
    cur = conn.cursor()

    for v, site in SITES_REGISTRY.items():
        cur.execute("""
            INSERT INTO sites (site_id, site_name, site_type, vpn_ip)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (site_id) DO UPDATE SET
                site_name = EXCLUDED.site_name,
                site_type = EXCLUDED.site_type,
                vpn_ip    = EXCLUDED.vpn_ip,
                updated_at = CURRENT_TIMESTAMP;
        """, (site["site_id"], site["site_name"], site["site_type"], site["vpn_ip"]))

    cur.close()
    conn.close()


def save_document(
    ville: str, filename: str, archive_path: str, file_type: str, nb_pages: int,
    source_site_id: int | None = None, is_primary: bool = True,
    replication_status_override: str | None = None, checksum: str | None = None,
    project_bucket: str | None = None,
) -> int:
    from config import SITES_REGISTRY, get_replication_policy

    validate_ville(ville)
    current_site_id = SITES_REGISTRY[ville]["site_id"]

    effective_source_site_id = source_site_id if source_site_id is not None else current_site_id

    if is_primary:
        policy = get_replication_policy(ville)
        status = "PENDING" if policy != "LOCAL_ONLY" else "SYNCED"
    else:
        policy = "LOCAL_ONLY"
        status = "SYNCED"

    if replication_status_override:
        status = replication_status_override

    conn = get_conn(ville)
    conn.autocommit = False
    cur = conn.cursor()

    try:
        cur.execute("""
            INSERT INTO documents
                (filename, archive_path, file_type, nb_pages,
                 source_site_id, current_site_id,
                 replication_status, replication_policy, is_primary,
                 checksum, archive_state, project_bucket)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id_doc;
        """, (
            filename, archive_path, file_type, nb_pages,
            effective_source_site_id, current_site_id,
            status, policy, is_primary, checksum, "ARCHIVED", project_bucket,
        ))

        id_doc = cur.fetchone()[0]
        conn.commit()
        return id_doc

    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


def get_documents(ville: str, limit: int = 20, file_type: str | None = None,
                  include_deleted: bool = False,
                  project_bucket_filter: str | None = "__NONE__") -> list[dict]:
    """
    project_bucket_filter :
      "__NONE__" (défaut) → ne filtre pas (usage interne)
      "ALL"               → admin, aucun filtre
      None                → seulement les documents legacy (sans projet)
      "documents-projetX" → seulement ce bucket-projet précis
    """
    validate_ville(ville)
    conn = get_conn(ville)
    cur = conn.cursor()

    conditions = []
    params = []

    if not include_deleted:
        conditions.append("archive_state != 'DELETED'")
    if file_type:
        conditions.append("file_type = %s")
        params.append(file_type)

    if project_bucket_filter == "ALL":
        pass
    elif project_bucket_filter is None:
        conditions.append("project_bucket IS NULL")
    elif project_bucket_filter != "__NONE__":
        conditions.append("project_bucket = %s")
        params.append(project_bucket_filter)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.append(limit)

    cur.execute(f"""
        SELECT id_doc, filename, archive_path, file_type, nb_pages, created_at,
               archive_state, project_bucket
        FROM documents {where}
        ORDER BY created_at DESC LIMIT %s;
    """, params)

    rows = cur.fetchall()
    cur.close()
    conn.close()

    return [
        {
            "ville": ville, "id_doc": r[0], "filename": r[1], "archive_path": r[2],
            "file_type": r[3], "nb_pages": r[4], "created_at": r[5].isoformat(),
            "archive_state": r[6], "project_bucket": r[7],
        }
        for r in rows
    ]


def get_document_by_id(ville: str, id_doc: int) -> tuple[str, str] | None:
    validate_ville(ville)
    conn = get_conn(ville)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT filename, archive_path FROM documents WHERE id_doc=%s AND archive_state != 'DELETED'",
            (id_doc,),
        )
        row = cur.fetchone()
    finally:
        cur.close()
        conn.close()
    return row


def get_document_full(ville: str, id_doc: int) -> dict | None:
    validate_ville(ville)
    conn = get_conn(ville)
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id_doc, filename, archive_path, replication_status,
                   replication_policy, backup_ville, backup_archive_path,
                   is_primary, checksum, archive_state, project_bucket
            FROM documents WHERE id_doc=%s
        """, (id_doc,))
        row = cur.fetchone()
    finally:
        cur.close()
        conn.close()

    if not row:
        return None

    return {
        "id_doc": row[0], "filename": row[1], "archive_path": row[2],
        "replication_status": row[3], "replication_policy": row[4],
        "backup_ville": row[5], "backup_archive_path": row[6],
        "is_primary": row[7], "checksum": row[8], "archive_state": row[9],
        "project_bucket": row[10],
    }


def get_document_by_archive_path(ville: str, archive_path: str) -> dict | None:
    """Retrouve un document par son chemin MinIO — utilisé pour la
    suppression de copies de secours (ID différent entre sites)."""
    validate_ville(ville)
    conn = get_conn(ville)
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id_doc, filename, archive_path, replication_status,
                   replication_policy, backup_ville, backup_archive_path,
                   is_primary, checksum, archive_state, project_bucket
            FROM documents WHERE archive_path=%s
        """, (archive_path,))
        row = cur.fetchone()
    finally:
        cur.close()
        conn.close()

    if not row:
        return None

    return {
        "id_doc": row[0], "filename": row[1], "archive_path": row[2],
        "replication_status": row[3], "replication_policy": row[4],
        "backup_ville": row[5], "backup_archive_path": row[6],
        "is_primary": row[7], "checksum": row[8], "archive_state": row[9],
        "project_bucket": row[10],
    }


def get_pending_replications(ville: str) -> list[dict]:
    validate_ville(ville)
    conn = get_conn(ville)
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id_doc, filename, archive_path, file_type, replication_policy,
                   nb_pages, project_bucket
            FROM documents
            WHERE replication_status IN ('PENDING', 'FAILED')
              AND replication_policy != 'LOCAL_ONLY'
            ORDER BY created_at ASC;
        """)
        rows = cur.fetchall()
    finally:
        cur.close()
        conn.close()

    return [
        {
            "id_doc": r[0], "filename": r[1], "archive_path": r[2],
            "file_type": r[3], "replication_policy": r[4], "nb_pages": r[5],
            "project_bucket": r[6],
        }
        for r in rows
    ]


def update_replication_status(
    ville: str, id_doc: int, status: str,
    backup_ville: str | None = None, backup_archive_path: str | None = None,
):
    validate_ville(ville)
    conn = get_conn(ville)
    conn.autocommit = True
    cur = conn.cursor()

    if status == "SYNCED":
        cur.execute("""
            UPDATE documents
            SET replication_status = %s, backup_ville = %s,
                backup_archive_path = %s, replicated_at = CURRENT_TIMESTAMP
            WHERE id_doc = %s;
        """, (status, backup_ville, backup_archive_path, id_doc))
    else:
        cur.execute("""
            UPDATE documents SET replication_status = %s WHERE id_doc = %s;
        """, (status, id_doc))

    cur.close()
    conn.close()


def soft_delete_document(ville: str, id_doc: int) -> bool:
    validate_ville(ville)
    conn = get_conn(ville)
    conn.autocommit = True
    cur = conn.cursor()

    cur.execute("""
        UPDATE documents SET archive_state = 'DELETED'
        WHERE id_doc = %s AND archive_state != 'DELETED'
        RETURNING id_doc;
    """, (id_doc,))
    row = cur.fetchone()

    cur.close()
    conn.close()
    return row is not None


def delete_document(ville: str, id_doc: int) -> str | None:
    validate_ville(ville)
    conn = get_conn(ville)
    conn.autocommit = True
    cur = conn.cursor()

    cur.execute("""
        DELETE FROM documents WHERE id_doc = %s RETURNING archive_path;
    """, (id_doc,))
    row = cur.fetchone()

    cur.close()
    conn.close()
    return row[0] if row else None


def rename_document(ville: str, id_doc: int, new_filename: str) -> bool:
    validate_ville(ville)
    conn = get_conn(ville)
    conn.autocommit = True
    cur = conn.cursor()

    cur.execute("""
        UPDATE documents SET filename = %s
        WHERE id_doc = %s AND archive_state != 'DELETED'
        RETURNING id_doc;
    """, (new_filename, id_doc))
    row = cur.fetchone()

    cur.close()
    conn.close()
    return row is not None