-- Canonical core schema, applied transactionally before serving.
BEGIN IMMEDIATE;

CREATE TABLE core_schema_version (version INTEGER NOT NULL);
INSERT INTO core_schema_version VALUES (1);
CREATE TABLE publication (id TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE revision (
 id TEXT PRIMARY KEY, publication_id TEXT NOT NULL, data TEXT NOT NULL,
 state TEXT NOT NULL, retired_at TEXT, drain_until TEXT, recovery_until TEXT
);
CREATE INDEX revision_publication ON revision(publication_id);
CREATE TABLE revision_file (
 revision_id TEXT NOT NULL, path TEXT NOT NULL, data TEXT NOT NULL,
 PRIMARY KEY(revision_id,path)
);
CREATE TABLE upload (
 id TEXT PRIMARY KEY, subject TEXT NOT NULL, data TEXT NOT NULL,
 state TEXT NOT NULL, deadline TEXT NOT NULL, hard_deadline TEXT NOT NULL,
 reservation_bytes INTEGER NOT NULL, received_bytes INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE read_lease (
 id TEXT PRIMARY KEY, revision_id TEXT NOT NULL, kind TEXT NOT NULL,
 deadline TEXT NOT NULL, hard_deadline TEXT NOT NULL
);
CREATE INDEX lease_revision ON read_lease(revision_id);
CREATE TABLE core_settings (id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL);
CREATE TABLE deletion_tombstone (publication_id TEXT PRIMARY KEY, deleted_at TEXT NOT NULL);

COMMIT;
