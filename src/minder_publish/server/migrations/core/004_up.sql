BEGIN IMMEDIATE;
-- Separate owner aggregates preserve the established guest metric and its history.
-- IF NOT EXISTS also supports re-upgrading the retained schema-3 extension.
CREATE TABLE IF NOT EXISTS owner_statistics_collection (
 id INTEGER PRIMARY KEY CHECK(id=1), started_at TEXT NOT NULL,
 interrupted INTEGER NOT NULL DEFAULT 0 CHECK(interrupted IN (0,1)), interrupted_at TEXT
);
INSERT OR IGNORE INTO owner_statistics_collection(id,started_at)
 VALUES(1,strftime('%Y-%m-%dT%H:%M:%fZ','now'));
UPDATE owner_statistics_collection SET interrupted_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
 WHERE interrupted=1;
CREATE TABLE IF NOT EXISTS publication_owner_statistics (
 publication_id TEXT PRIMARY KEY REFERENCES publication(id) ON DELETE CASCADE,
 started_at TEXT NOT NULL, total INTEGER NOT NULL DEFAULT 0,
 last_opened TEXT, incomplete INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS publication_owner_statistics_daily (
 publication_id TEXT NOT NULL REFERENCES publication(id) ON DELETE CASCADE,
 day TEXT NOT NULL, opens INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(publication_id,day)
);
CREATE INDEX IF NOT EXISTS owner_statistics_day ON publication_owner_statistics_daily(day);
UPDATE publication SET data=json_set(data,'$.owner_counts',json('true'))
 WHERE json_type(data,'$.owner_counts') IS NULL;
UPDATE core_schema_version SET version=4;
COMMIT;
