"""Bounded guest/owner aggregates, committed with one-use handoff consumption."""

from datetime import timedelta

from minder_publish.errors import PublishError

from .core import iso, now, parse


class Statistics:
    def __init__(self, core):
        self.core = core

    def cleanup(self, connection, timestamp=None):
        timestamp = timestamp or now()
        cutoff = (timestamp.date() - timedelta(days=self.core.config.operations["stats_retention_days"] - 1)).isoformat()
        connection.execute("DELETE FROM publication_statistics_daily WHERE day<?", (cutoff,))
        connection.execute("DELETE FROM publication_owner_statistics_daily WHERE day<?", (cutoff,))

    def totals(self, identifiers):
        if not identifiers:
            return {}
        placeholders = ",".join("?" for _ in identifiers)
        with self.core.repository.connect() as connection:
            return {row["id"]: {"guest_open_count": row["guest"], "owner_open_count": row["owner"]} for row in connection.execute(
                f"SELECT p.id,COALESCE(g.total,0) AS guest,COALESCE(o.total,0) AS owner FROM publication p "
                "LEFT JOIN publication_statistics g ON g.publication_id=p.id "
                "LEFT JOIN publication_owner_statistics o ON o.publication_id=p.id "
                f"WHERE p.id IN ({placeholders})", tuple(identifiers),
            )}

    def _owner_start(self, connection, publication):
        started = connection.execute("SELECT started_at FROM owner_statistics_collection WHERE id=1").fetchone()[0]
        created = publication.get("created_at", started)
        return iso(max(parse(started), parse(created)))

    def record(self, connection, payload):
        owner = bool(payload.get("count_owner_opening"))
        if not owner and not payload.get("count_guest_opening"):
            return
        identifier = payload["publication_id"]
        publication = self.core._publication(connection, identifier)
        if not publication["owner_counts" if owner else "guest_counts"] or publication.get("deletion_state"):
            return
        timestamp = now()
        day = timestamp.date().isoformat()
        # Table names are internal constants, never request values.
        aggregate = "publication_owner_statistics" if owner else "publication_statistics"
        daily = aggregate + "_daily"
        if owner:
            connection.execute(
                "INSERT OR IGNORE INTO publication_owner_statistics(publication_id,started_at) VALUES(?,?)",
                (identifier, self._owner_start(connection, publication)),
            )
        else:
            connection.execute(
                "INSERT OR IGNORE INTO publication_statistics(publication_id,started_at,legacy_total) VALUES(?,?,?)",
                (identifier, iso(timestamp), publication.get("view_count", 0)),
            )
        connection.execute(f"UPDATE {aggregate} SET total=total+1,last_opened=? WHERE publication_id=?", (iso(timestamp), identifier))
        self.cleanup(connection, timestamp)
        exists = connection.execute(f"SELECT 1 FROM {daily} WHERE publication_id=? AND day=?", (identifier, day)).fetchone()
        has_capacity = bool(exists)
        if not has_capacity:
            count = connection.execute(
                "SELECT (SELECT COUNT(*) FROM publication_statistics_daily)+(SELECT COUNT(*) FROM publication_owner_statistics_daily)"
            ).fetchone()[0]
            has_capacity = count < self.core.config.operations["stats_daily_row_limit"]
        if has_capacity:
            connection.execute(
                f"INSERT INTO {daily} VALUES(?,?,1) ON CONFLICT(publication_id,day) DO UPDATE SET opens=opens+1", (identifier, day),
            )
        else:
            connection.execute(f"UPDATE {aggregate} SET incomplete=1 WHERE publication_id=?", (identifier,))
        if not owner:
            publication["view_count"] = publication.get("view_count", 0) + 1
            publication["last_viewed"] = iso(timestamp)
            self.core._save_publication(connection, publication)

    def _read_aggregate(self, connection, publication, days, *, owner):
        aggregate = "publication_owner_statistics" if owner else "publication_statistics"
        identifier = publication["id"]
        row = connection.execute(f"SELECT * FROM {aggregate} WHERE publication_id=?", (identifier,)).fetchone()
        timestamp = now()
        start = timestamp.date() - timedelta(days=days - 1)
        cutoff = timestamp.date() - timedelta(days=self.core.config.operations["stats_retention_days"] - 1)
        values = dict(connection.execute(f"SELECT day,opens FROM {aggregate}_daily WHERE publication_id=? AND day>=?", (identifier, max(start, cutoff).isoformat())).fetchall())
        started_at = row["started_at"] if row else self._owner_start(connection, publication) if owner else None
        interrupted = False
        if owner:
            collection = connection.execute("SELECT interrupted,interrupted_at FROM owner_statistics_collection WHERE id=1").fetchone()
            interrupted = bool(collection["interrupted"] and parse(started_at) <= parse(collection["interrupted_at"]))
        unknown_missing = owner and (interrupted or bool(row and row["incomplete"]))
        daily = []
        for index in range(days):
            date = start + timedelta(days=index)
            # Owner collection has an explicit lower bound. Earlier days are unknown,
            # not historical zeroes; retained-out daily history is unknown as well.
            available = not owner or (date >= parse(started_at).date() and date >= cutoff)
            daily.append({"day": date.isoformat(), "opens": values.get(date.isoformat(), None if unknown_missing else 0) if available else None})
        result = {"total": row["total"] if row else 0, "started_at": started_at, "last_opened": row["last_opened"] if row else None, "period_total": sum(item["opens"] or 0 for item in daily), "daily": daily, "enabled": publication["owner_counts" if owner else "guest_counts"], "incomplete": bool(row and row["incomplete"])}
        if owner:
            result["collection_interrupted"] = interrupted
        else:
            result["legacy_total"] = row["legacy_total"] if row else publication.get("view_count", 0)
        return result

    def read(self, identifier, days=30):
        if days not in (7, 30, 90):
            raise PublishError("invalid_statistics_period", 400, field="days")
        with self.core.repository.connect() as connection:
            publication = self.core._publication(connection, identifier)
            if publication.get("deletion_state"):
                raise PublishError("publication_not_found", 404)
            result = self._read_aggregate(connection, publication, days, owner=False)
            result["owner"] = self._read_aggregate(connection, publication, days, owner=True)
            result["timezone"] = "UTC"
            return result

    def clear(self, identifier, authorize):
        with self.core.repository.connect(immediate=True) as connection:
            authorize(connection)
            publication = self.core._publication(connection, identifier)
            if publication.get("deletion_state"):
                raise PublishError("publication_not_found", 404)
            connection.execute("DELETE FROM publication_statistics_daily WHERE publication_id=?", (identifier,))
            connection.execute("DELETE FROM publication_statistics WHERE publication_id=?", (identifier,))
            connection.execute("DELETE FROM publication_owner_statistics_daily WHERE publication_id=?", (identifier,))
            connection.execute("DELETE FROM publication_owner_statistics WHERE publication_id=?", (identifier,))
            # A reset starts a new measured owner interval for this material.
            connection.execute("INSERT INTO publication_owner_statistics(publication_id,started_at) VALUES(?,?)", (identifier, iso(now())))
            publication["view_count"] = 0
            publication["last_viewed"] = None
            self.core._save_publication(connection, publication)
