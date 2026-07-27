"""Coordinates for the quays the archive already knows about.

The realtime feed identifies stops by reference only, which is enough to
train on and useless to draw. JourneyPlanner resolves those references to
positions, so the network can be drawn from our own archive rather than
borrowed from a background map.

Fetched once and stored; quays do not move.
"""

import argparse
import sys
from datetime import datetime, timezone

from . import db, net
from .config import GRAPHQL_URL

BATCH = 250

QUERY = """
query Quays($ids: [String]!) {
  quays(ids: $ids) {
    id
    name
    latitude
    longitude
    stopPlace { id name }
  }
}
"""


def missing_quays(conn):
    return [
        row[0] for row in conn.execute(
            "SELECT DISTINCT c.stop_ref FROM call_snapshot c "
            "LEFT JOIN quay q ON q.quay_ref = c.stop_ref "
            "WHERE c.stop_ref IS NOT NULL AND q.quay_ref IS NULL"
        )
    ]


def fetch(ids):
    data = net.post_json(GRAPHQL_URL, {"query": QUERY, "variables": {"ids": ids}})
    if "errors" in data:
        raise RuntimeError(str(data["errors"])[:300])
    return [q for q in (data.get("data") or {}).get("quays") or [] if q]


def refresh(conn, log=print):
    """Resolve every stop reference in the archive that has no position yet."""
    pending = missing_quays(conn)
    if not pending:
        return 0
    log(f"quays: resolving {len(pending)}")
    now = datetime.now(timezone.utc).isoformat()
    stored = 0
    for start in range(0, len(pending), BATCH):
        chunk = pending[start:start + BATCH]
        rows = [
            (q["id"], q.get("name"), q.get("latitude"), q.get("longitude"),
             (q.get("stopPlace") or {}).get("id"),
             (q.get("stopPlace") or {}).get("name"), now)
            for q in fetch(chunk)
            if q.get("latitude") is not None and q.get("longitude") is not None
        ]
        conn.executemany(
            "INSERT INTO quay (quay_ref, name, lat, lon, stop_place_ref, "
            "stop_place_name, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(quay_ref) DO UPDATE SET lat = excluded.lat, "
            "lon = excluded.lon, name = excluded.name",
            rows,
        )
        conn.commit()
        stored += len(rows)
        log(f"  {min(start + BATCH, len(pending))}/{len(pending)} ({stored} med posisjon)")
    return stored


def main(argv=None):
    parser = argparse.ArgumentParser(description="Resolve stop references to positions")
    parser.parse_args(argv)
    from .config import DB_PATH

    conn = db.connect(DB_PATH)
    try:
        stored = refresh(conn)
        total = conn.execute("SELECT COUNT(*) FROM quay").fetchone()[0]
        print(f"stored {stored}, {total} quays known")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
