ALTER TABLE auth_sessions ADD COLUMN absolute_expires_at REAL NOT NULL DEFAULT 0;
ALTER TABLE auth_sessions ADD COLUMN last_refreshed_at REAL NOT NULL DEFAULT 0;
ALTER TABLE auth_sessions ADD COLUMN authenticated_at REAL NOT NULL DEFAULT 0;
CREATE INDEX auth_sessions_absolute_expiry ON auth_sessions(absolute_expires_at);
UPDATE auth_schema_version SET version=2 WHERE id=1;
