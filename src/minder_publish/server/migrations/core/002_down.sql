BEGIN IMMEDIATE;
DROP TABLE external_reservation;
UPDATE core_schema_version SET version=1;
COMMIT;
