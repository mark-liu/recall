#!/usr/bin/env python3
"""Search past Claude Code and Codex sessions using FTS5 full-text search.

Search-only CLI. The Rust indexer (`recall-indexer-rs` from `mark-liu/recall-rs`)
is the sole writer of `~/.recall.db` — this script never opens the DB for write
and never bootstraps the schema. If the DB is missing, run the indexer.
"""

import argparse
import math
import re
import sqlite3
import sys
import time
from pathlib import Path

DB_PATH = Path.home() / ".recall.db"

CJK_RE = re.compile(
    r"[⺀-鿿가-힯豈-﫿"
    r"\U00020000-\U0002A6DF\U0002A700-\U0002B73F"
    r"\U0002B740-\U0002B81F\U0002B820-\U0002CEAF"
    r"\U0002CEB0-\U0002EBEF\U00030000-\U0003134F]"
)


def has_cjk(text):
    """Return True if text contains any CJK characters."""
    return bool(CJK_RE.search(text))


def sanitize_fts_query(query):
    """Sanitize a query for FTS5 MATCH.

    FTS5 interprets bare hyphens as the NOT operator, so 'ask-codex' becomes
    'ask NOT codex' which errors out when 'codex' isn't a column name.
    Fix: split hyphenated words into individually quoted segments so
    'ask-codex' -> '"ask" "codex"' (proximity match, no boolean interpretation).
    User-quoted phrases and explicit boolean operators are preserved.
    """
    parts = []
    in_quote = False
    for segment in query.split('"'):
        if in_quote:
            parts.append(f'"{segment}"')
        else:
            segment = re.sub(
                r"\b(\w+(?:-\w+)+)\b",
                lambda m: " ".join(f'"{w}"' for w in m.group().split("-")),
                segment,
            )
            parts.append(segment)
        in_quote = not in_quote
    return "".join(parts)


def search(conn, query, project=None, days=None, source=None, limit=10):
    """Search indexed sessions. Uses trigram table for CJK queries, porter table otherwise."""
    use_cjk = has_cjk(query)
    fts_table = "messages_cjk" if use_cjk else "messages"
    use_like = use_cjk and len(query.strip()) < 3

    session_filter_conds = []
    filter_params = []
    if project:
        session_filter_conds.append("s2.project LIKE ? || '%'")
        filter_params.append(project)
    if days:
        cutoff = int((time.time() - days * 86400) * 1000)
        session_filter_conds.append("s2.timestamp >= ?")
        filter_params.append(cutoff)
    if source:
        session_filter_conds.append("s2.source = ?")
        filter_params.append(source)

    session_filter = ""
    if session_filter_conds:
        session_filter = (
            " AND session_id IN "
            "(SELECT s2.session_id FROM sessions s2 WHERE "
            + " AND ".join(session_filter_conds)
            + ")"
        )

    candidate_limit = limit * 3

    if use_like:
        like_params = [f"%{query}%"] + filter_params + [candidate_limit]
        like_sql = f"""
            SELECT session_id, -1.0 as best_rank
            FROM messages_cjk
            WHERE text LIKE ?{session_filter}
            GROUP BY session_id
            LIMIT ?
        """
        try:
            ranked = conn.execute(like_sql, like_params).fetchall()
        except sqlite3.OperationalError as e:
            print(f"Search error: {e}", file=sys.stderr)
            return []
    else:
        sanitized = sanitize_fts_query(query)
        fts_params = [sanitized] + filter_params + [candidate_limit]
        inner_sql = f"""
            SELECT session_id, MIN(rank) as best_rank
            FROM {fts_table}
            WHERE {fts_table} MATCH ?{session_filter}
            GROUP BY session_id
            ORDER BY best_rank
            LIMIT ?
        """
        try:
            ranked = conn.execute(inner_sql, fts_params).fetchall()
        except sqlite3.OperationalError as e:
            print(f"Search error: {e}", file=sys.stderr)
            return []

    results = []
    now_ms = time.time() * 1000
    for session_id, rank in ranked:
        meta = conn.execute(
            "SELECT source, file_path, project, slug, timestamp FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if not meta:
            continue

        if use_like:
            snippet_row = conn.execute(
                "SELECT text FROM messages_cjk WHERE text LIKE ? AND session_id = ? LIMIT 1",
                (f"%{query}%", session_id),
            ).fetchone()
            excerpt = snippet_row[0] if snippet_row else ""
        else:
            snippet_row = conn.execute(
                f"SELECT snippet({fts_table}, 2, '**', '**', '...', 20) FROM {fts_table} WHERE {fts_table} MATCH ? AND session_id = ? LIMIT 1",
                (sanitized, session_id),
            ).fetchone()
            excerpt = snippet_row[0] if snippet_row else ""

        timestamp = meta[4]
        if timestamp:
            age_days = max((now_ms - timestamp) / 86_400_000, 0)
            recency_boost = math.exp(-0.693 * age_days / 30)
        else:
            recency_boost = 0.0
        blended_rank = rank * (1 - 0.2 * recency_boost)

        results.append(
            (
                session_id,
                meta[0],
                meta[1],
                meta[2],
                meta[3],
                meta[4],
                excerpt,
                blended_rank,
            )
        )

    results.sort(key=lambda r: r[7])
    return results[:limit]


def format_timestamp(ts_ms):
    """Format millisecond timestamp to date string."""
    if not ts_ms:
        return "unknown"
    try:
        ts = float(ts_ms) / 1000
        return time.strftime("%Y-%m-%d", time.localtime(ts))
    except (OSError, ValueError, TypeError):
        return "unknown"


def show_stats(conn, days=None, source=None):
    """Show token usage statistics across sessions."""
    conds = []
    params = []
    if days:
        cutoff = int((time.time() - days * 86400) * 1000)
        conds.append("timestamp >= ?")
        params.append(cutoff)
    if source:
        conds.append("source = ?")
        params.append(source)

    where = f" WHERE {' AND '.join(conds)}" if conds else ""

    row = conn.execute(
        f"""
        SELECT COUNT(*), SUM(input_tokens), SUM(output_tokens),
               SUM(cache_read_tokens), SUM(cache_create_tokens)
        FROM sessions{where}
    """,
        params,
    ).fetchone()

    total, inp, out, cache_r, cache_c = row
    inp = inp or 0
    out = out or 0
    cache_r = cache_r or 0
    cache_c = cache_c or 0

    scope = []
    if days:
        scope.append(f"last {days}d")
    if source:
        scope.append(source)
    scope_str = f" ({', '.join(scope)})" if scope else ""

    print(f"Token stats{scope_str}: {total} sessions")
    print(f"  Input:        {inp:>12,}")
    print(f"  Output:       {out:>12,}")
    print(f"  Cache read:   {cache_r:>12,}")
    print(f"  Cache create: {cache_c:>12,}")
    print(f"  Total:        {inp + out:>12,}")

    top = conn.execute(
        f"""
        SELECT slug, project, input_tokens + output_tokens as total,
               input_tokens, output_tokens, timestamp
        FROM sessions{where}
        ORDER BY total DESC LIMIT 10
    """,
        params,
    ).fetchall()

    if top:
        print("\nTop 10 sessions by token usage:")
        for slug, project, total_tok, inp_tok, out_tok, ts in top:
            date = format_timestamp(ts)
            proj = Path(project).name if project else "?"
            print(
                f"  {date} | {slug:<20} | {proj:<15} | in:{inp_tok:>8,} out:{out_tok:>8,} = {total_tok:>9,}"
            )


def open_db():
    """Open the recall database read-only.

    Returns None if the DB doesn't exist — caller should suggest running the
    Rust indexer (`recall-indexer-rs`) which creates and writes it.
    """
    if not DB_PATH.exists():
        return None
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.execute("PRAGMA busy_timeout=2000")
    return conn


def main():
    parser = argparse.ArgumentParser(
        description="Search past Claude Code and Codex sessions"
    )
    parser.add_argument(
        "query",
        nargs="?",
        help="Search query (FTS5 syntax: quotes for phrases, AND/OR/NOT)",
    )
    parser.add_argument(
        "--project",
        help="Filter to sessions from a specific project path (prefix match)",
    )
    parser.add_argument("--days", type=int, help="Only sessions from last N days")
    parser.add_argument(
        "--source",
        choices=["claude", "codex"],
        help="Filter by source (claude or codex)",
    )
    parser.add_argument(
        "--limit", type=int, default=10, help="Max results (default: 10)"
    )
    parser.add_argument(
        "--stats", action="store_true", help="Show token usage statistics"
    )

    args = parser.parse_args()

    if not args.query and not args.stats:
        parser.error("query is required (or use --stats)")

    conn = open_db()
    if conn is None:
        print(
            f"recall.db not found at {DB_PATH}. Run recall-indexer-rs first.",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.stats:
        show_stats(conn, days=args.days, source=args.source)
        conn.close()
        return

    total_sessions = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]

    results = search(
        conn,
        args.query,
        project=args.project,
        days=args.days,
        source=args.source,
        limit=args.limit,
    )

    if not results:
        print("No matching sessions found.")
        conn.close()
        return

    print(f"Found {len(results)} sessions (index: {total_sessions} sessions):\n")

    for i, (
        session_id,
        source,
        file_path,
        project,
        slug,
        timestamp,
        excerpt,
        rank,
    ) in enumerate(results, 1):
        date = format_timestamp(timestamp)
        src_tag = f"[{source}]" if source else ""
        proj_name = Path(project).name if project else "unknown"
        print(f"[{i}] {date} | {slug} | {proj_name} {src_tag}")
        if project:
            print(f"    {project}")
        print(f"    ID: {session_id}")
        if file_path:
            print(f"    File: {file_path}")
        if excerpt:
            excerpt_clean = excerpt.replace("\n", " ").strip()
            if len(excerpt_clean) > 200:
                excerpt_clean = excerpt_clean[:200] + "..."
            print(f"    > {excerpt_clean}")
        print()

    conn.close()


if __name__ == "__main__":
    main()
