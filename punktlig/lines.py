"""Fetch line metadata (id -> transport mode) from Entur's JourneyPlanner GraphQL API.

The realtime feed does not reliably tag journeys with a transport mode, so the
mode filter (tram/metro/...) is resolved through this lookup table instead.
"""

from datetime import datetime, timezone

from . import net
from .config import AUTHORITIES, GRAPHQL_URL

QUERY = """
query Lines($authorities: [String]) {
  lines(authorities: $authorities) {
    id
    publicCode
    name
    transportMode
  }
}
"""


def refresh_lines(conn, authorities=None):
    """Refresh the line to mode lookup for every configured authority."""
    data = net.post_json(
        GRAPHQL_URL,
        {"query": QUERY, "variables": {"authorities": authorities or AUTHORITIES}},
    )
    lines = data["data"]["lines"]
    now = datetime.now(timezone.utc).isoformat()
    conn.executemany(
        "INSERT INTO line (line_ref, mode, public_code, name, fetched_at) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(line_ref) DO UPDATE SET mode = excluded.mode, public_code = excluded.public_code, "
        "name = excluded.name, fetched_at = excluded.fetched_at",
        [(l["id"], l["transportMode"], l.get("publicCode"), l.get("name"), now) for l in lines],
    )
    conn.commit()
    return len(lines)


def line_modes(conn):
    return dict(conn.execute("SELECT line_ref, mode FROM line"))
