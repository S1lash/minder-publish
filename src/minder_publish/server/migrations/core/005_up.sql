BEGIN IMMEDIATE;
CREATE TABLE publication_link (
 name TEXT PRIMARY KEY NOT NULL,
 publication_id TEXT NOT NULL,
 is_primary INTEGER NOT NULL CHECK (is_primary IN (0, 1)),
 created_at TEXT NOT NULL,
 changed_at TEXT NOT NULL,
 retired_at TEXT
);
CREATE UNIQUE INDEX publication_link_primary ON publication_link(publication_id)
 WHERE is_primary=1;
CREATE INDEX publication_link_owner ON publication_link(publication_id, retired_at);
WITH existing AS (
 SELECT p.id,COALESCE(json_extract(p.data,'$.created_at'),strftime('%Y-%m-%dT%H:%M:%fZ','now')) AS created,
        COALESCE(t.deleted_at,CASE WHEN json_extract(p.data,'$.deletion_state') IS NOT NULL
            THEN strftime('%Y-%m-%dT%H:%M:%fZ','now') END) AS retired
 FROM publication p LEFT JOIN deletion_tombstone t ON t.publication_id=p.id
)
INSERT INTO publication_link(name,publication_id,is_primary,created_at,changed_at,retired_at)
 SELECT id,id,1,created,COALESCE(retired,created),retired FROM existing;
INSERT INTO publication_link(name,publication_id,is_primary,created_at,changed_at,retired_at)
 SELECT publication_id,publication_id,1,deleted_at,deleted_at,deleted_at FROM deletion_tombstone
 WHERE NOT EXISTS (SELECT 1 FROM publication WHERE id=deletion_tombstone.publication_id);
CREATE INDEX upload_publication_link_key ON upload(json_extract(data,'$.publication.id'))
 WHERE state NOT IN ('committed','aborted','failed');
UPDATE core_schema_version SET version=5;
COMMIT;
