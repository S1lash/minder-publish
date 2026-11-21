-- Explicit operator-only destructive rollback on a disposable migration target.
BEGIN IMMEDIATE;
DROP TABLE deletion_tombstone;
DROP TABLE core_settings;
DROP TABLE read_lease;
DROP TABLE upload;
DROP TABLE revision_file;
DROP TABLE revision;
DROP TABLE publication;
DROP TABLE core_schema_version;
COMMIT;
