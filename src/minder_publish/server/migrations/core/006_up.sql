BEGIN IMMEDIATE;
ALTER TABLE publication_link ADD COLUMN preview_mode TEXT NOT NULL DEFAULT 'inherit'
 CHECK (preview_mode IN ('inherit', 'on', 'off'));
UPDATE core_schema_version SET version=6;
COMMIT;
