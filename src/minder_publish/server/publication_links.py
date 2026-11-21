"""Names reserved for stable publication links."""

from minder_publish.errors import PublishError

from .storage import safe_id


def add_initial(connection, publication):
    add_primary(connection, publication["id"], publication["id"], publication["created_at"])


def add_primary(connection, name, publication_id, timestamp):
    connection.execute(
        """INSERT INTO publication_link(name,publication_id,is_primary,created_at,changed_at)
           VALUES (?,?,1,?,?)""",
        (name, publication_id, timestamp, timestamp),
    )


def retire_all(connection, publication_id, retired_at):
    connection.execute(
        """UPDATE publication_link SET retired_at=?,changed_at=?
           WHERE publication_id=? AND retired_at IS NULL""",
        (retired_at, retired_at, publication_id),
    )


def record_deletion(connection, publication_id, timestamp):
    connection.execute(
        "INSERT OR IGNORE INTO deletion_tombstone VALUES (?,?)",
        (publication_id, timestamp),
    )
    retire_all(connection, publication_id, timestamp)
    connection.execute(
        """INSERT OR IGNORE INTO publication_link
           (name,publication_id,is_primary,created_at,changed_at,retired_at)
           VALUES (?,?,0,?,?,?)""",
        (publication_id, publication_id, timestamp, timestamp, timestamp),
    )


def occupied(connection, name, *, exclude_upload=None):
    return connection.execute(
        """SELECT 1 FROM publication_link WHERE name=?
           UNION ALL SELECT 1 FROM publication WHERE id=?
           UNION ALL SELECT 1 FROM deletion_tombstone WHERE publication_id=?
           UNION ALL SELECT 1 FROM upload
             WHERE state NOT IN ('committed','aborted','failed')
             AND json_extract(data,'$.publication.id')=?
             AND id IS NOT ?
           LIMIT 1""",
        (name, name, name, name, exclude_upload),
    ).fetchone() is not None


def resolve(connection, name):
    try:
        safe_id(name)
    except PublishError as exc:
        raise PublishError("publication_not_found", 404) from exc
    row = connection.execute(
        """SELECT link.publication_id
           FROM publication_link link JOIN publication p ON p.id=link.publication_id
           WHERE link.name=? AND (link.retired_at IS NULL OR link.is_primary=1)""",
        (name,),
    ).fetchone()
    if not row:
        raise PublishError("publication_not_found", 404)
    return row["publication_id"]


def stable_url(base_url, name):
    return f"{base_url}/p/{name}"


def snapshot(connection, publication, base_url):
    primary = publication["link_name"]
    return {
        "id": publication["id"],
        "link_name": primary,
        "stable_url": stable_url(base_url, primary),
        "previous_links": [
            {
                "name": row["name"],
                "url": stable_url(base_url, row["name"]),
                "created_at": row["created_at"],
            }
            for row in connection.execute(
                """SELECT name,created_at FROM publication_link
                   WHERE publication_id=? AND is_primary=0 AND retired_at IS NULL
                   ORDER BY created_at DESC, name""",
                (publication["id"],),
            )
        ],
    }
