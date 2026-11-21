BEGIN IMMEDIATE;
CREATE TABLE revision_history (
 revision_id TEXT PRIMARY KEY,
 publication_id TEXT NOT NULL REFERENCES publication(id) ON DELETE CASCADE,
 sequence INTEGER NOT NULL,
 created_at TEXT NOT NULL,
 size_bytes INTEGER NOT NULL,
 change_summary TEXT NOT NULL DEFAULT '',
 baseline INTEGER NOT NULL DEFAULT 0,
 UNIQUE(publication_id, sequence)
);
CREATE INDEX history_publication ON revision_history(publication_id, sequence);
INSERT INTO revision_history(revision_id,publication_id,sequence,created_at,size_bytes,baseline)
 SELECT r.id,r.publication_id,
 ROW_NUMBER() OVER (PARTITION BY r.publication_id ORDER BY json_extract(r.data,'$.created_at'),r.id),
 json_extract(r.data,'$.created_at'),json_extract(r.data,'$.size_bytes'),1
 FROM revision r JOIN publication p ON p.id=r.publication_id;
UPDATE publication SET data=json_set(data,'$.history_sequence',COALESCE((SELECT MAX(sequence) FROM revision_history h WHERE h.publication_id=publication.id),0));
CREATE TABLE publication_statistics (
 publication_id TEXT PRIMARY KEY REFERENCES publication(id) ON DELETE CASCADE,
 started_at TEXT NOT NULL, legacy_total INTEGER NOT NULL DEFAULT 0,
 total INTEGER NOT NULL DEFAULT 0, last_opened TEXT, incomplete INTEGER NOT NULL DEFAULT 0
);
INSERT INTO publication_statistics(publication_id,started_at,legacy_total)
 SELECT id,strftime('%Y-%m-%dT%H:%M:%fZ','now'),COALESCE(json_extract(data,'$.view_count'),0) FROM publication;
CREATE TABLE publication_statistics_daily (
 publication_id TEXT NOT NULL REFERENCES publication(id) ON DELETE CASCADE,
 day TEXT NOT NULL, opens INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(publication_id,day)
);
CREATE INDEX statistics_day ON publication_statistics_daily(day);
UPDATE core_schema_version SET version=3;
COMMIT;
