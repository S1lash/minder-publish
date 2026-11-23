BEGIN IMMEDIATE;
ALTER TABLE publication_link DROP COLUMN preview_mode;
UPDATE core_schema_version SET version=5;
COMMIT;
