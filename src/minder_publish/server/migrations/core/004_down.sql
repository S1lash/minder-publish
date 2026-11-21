BEGIN IMMEDIATE;
-- Schema 3 ignores these independent extension tables. Retain them so a rollback
-- never erases owner aggregates or later publication/auth changes. Owner collection
-- during the older release is unknown, so re-upgrade reports a possible gap.
UPDATE owner_statistics_collection SET interrupted=1,interrupted_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1;
UPDATE core_schema_version SET version=3;
COMMIT;
