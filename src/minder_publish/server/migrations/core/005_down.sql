BEGIN IMMEDIATE;
DROP INDEX upload_publication_link_key;
DROP TABLE publication_link;
UPDATE core_schema_version SET version=4;
COMMIT;
