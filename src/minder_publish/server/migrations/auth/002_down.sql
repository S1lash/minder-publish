DROP INDEX IF EXISTS auth_sessions_absolute_expiry;
ALTER TABLE auth_sessions DROP COLUMN authenticated_at;
ALTER TABLE auth_sessions DROP COLUMN last_refreshed_at;
ALTER TABLE auth_sessions DROP COLUMN absolute_expires_at;
UPDATE auth_schema_version SET version=1 WHERE id=1;
