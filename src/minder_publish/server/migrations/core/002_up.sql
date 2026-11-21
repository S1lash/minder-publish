BEGIN IMMEDIATE;
CREATE TABLE external_reservation (
 id TEXT PRIMARY KEY, subject TEXT NOT NULL, kind TEXT NOT NULL,
 size_bytes INTEGER NOT NULL, deadline TEXT NOT NULL
);
UPDATE core_schema_version SET version=2;
COMMIT;
