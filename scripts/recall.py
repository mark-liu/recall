#!/usr/bin/env python3
"""Search past Claude Code and Codex sessions using FTS5 full-text search."""

import argparse
import json
import os
import re
import sqlite3
import sys
import math
import time
from datetime import datetime
from pathlib import Path

CLAUDE_DIR = Path.home() / ".claude"
CODEX_DIR = Path.home() / ".codex"
DB_PATH = Path.home() / ".recall.db"
CLAUDE_PROJECTS_DIR = CLAUDE_DIR / "projects"
CODEX_SESSIONS_DIR = CODEX_DIR / "sessions"


CJK_RE = re.compile(
    r"[\u2E80-\u9FFF\uAC00-\uD7AF\uF900-\uFAFF"
    r"\U00020000-\U0002A6DF\U0002A700-\U0002B73F"
    r"\U0002B740-\U0002B81F\U0002B820-\U0002CEAF"
    r"\U0002CEB0-\U0002EBEF\U00030000-\U0003134F]"
)


def has_cjk(text):
    """Return True if text contains any CJK characters."""
    return bool(CJK_RE.search(text))


SCHEMA_VERSION = 2  # bumped 2026-04-18: +has_rename +live_title +first_user_msg


def create_schema(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            source TEXT,
            file_path TEXT,
            project TEXT,
            slug TEXT,
            timestamp INTEGER,
            mtime REAL,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            cache_read_tokens INTEGER DEFAULT 0,
            cache_create_tokens INTEGER DEFAULT 0,
            has_rename INTEGER DEFAULT 0,
            live_title TEXT,
            first_user_msg TEXT
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS messages USING fts5(
            session_id UNINDEXED,
            role,
            text,
            tokenize='porter unicode61'
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS messages_cjk USING fts5(
            session_id UNINDEXED,
            role,
            text,
            tokenize='trigram'
        );
    """)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def migrate_schema(conn):
    """Add columns if upgrading from an older schema."""
    try:
        conn.execute("SELECT source FROM sessions LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE sessions ADD COLUMN source TEXT DEFAULT 'claude'")
        conn.commit()
    try:
        conn.execute("SELECT file_path FROM sessions LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE sessions ADD COLUMN file_path TEXT DEFAULT ''")
        conn.commit()
    needs_token_reindex = False
    for col, default in [
        ("input_tokens", 0),
        ("output_tokens", 0),
        ("cache_read_tokens", 0),
        ("cache_create_tokens", 0),
    ]:
        try:
            conn.execute(f"SELECT {col} FROM sessions LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute(
                f"ALTER TABLE sessions ADD COLUMN {col} INTEGER DEFAULT {default}"
            )
            conn.commit()
            needs_token_reindex = True

    if needs_token_reindex:
        # Mark all existing sessions dirty so they get re-parsed for token data.
        # Setting mtime to 0 ensures the indexer treats them as changed.
        conn.execute("UPDATE sessions SET mtime = 0")
        conn.commit()
        print("Token columns added — marked all sessions for reindex", file=sys.stderr)

    # v2 additions: has_rename, live_title, first_user_msg — consumed by
    # claude-resume (picker). Adding triggers a full reindex to populate.
    needs_v2_reindex = False
    for col, ddl in [
        ("has_rename", "INTEGER DEFAULT 0"),
        ("live_title", "TEXT"),
        ("first_user_msg", "TEXT"),
    ]:
        try:
            conn.execute(f"SELECT {col} FROM sessions LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute(f"ALTER TABLE sessions ADD COLUMN {col} {ddl}")
            conn.commit()
            needs_v2_reindex = True

    if needs_v2_reindex:
        conn.execute("UPDATE sessions SET mtime = 0")
        conn.commit()
        print("v2 columns added — marked all sessions for reindex", file=sys.stderr)

    # Stamp schema version (cheap even if no columns changed)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()


def migrate_db_location():
    """Move recall.db from ~/.claude/ to ~/ if it exists at the old path."""
    old_path = CLAUDE_DIR / "recall.db"
    if old_path.exists() and not DB_PATH.exists():
        old_path.rename(DB_PATH)
        # Also move the WAL/SHM files if they exist
        for suffix in ("-wal", "-shm"):
            old_extra = Path(str(old_path) + suffix)
            if old_extra.exists():
                old_extra.rename(Path(str(DB_PATH) + suffix))


TEXT_BLOCK_TYPES = {"text", "input_text", "output_text"}
CODEX_SKIP_MARKERS = (
    "<user_instructions>",
    "<environment_context>",
    "<permissions instructions>",
    "# AGENTS.md instructions",
)


def extract_text(content):
    """Extract plain text from message content (string or array format).

    Accepts "text" (Claude), "input_text" and "output_text" (Codex) block types.
    Skips tool calls, tool results, thinking blocks, and images.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type", "") in TEXT_BLOCK_TYPES
        ]
        return "\n".join(filter(None, parts))
    return ""


def parse_iso_timestamp(ts_str):
    """Parse ISO 8601 timestamp string to epoch milliseconds."""
    if not ts_str or not isinstance(ts_str, str):
        if isinstance(ts_str, (int, float)):
            return int(ts_str)
        return None
    try:
        # Handle "2026-03-03T00:26:57.352Z" format
        ts_str = ts_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts_str)
        return int(dt.timestamp() * 1000)
    except (ValueError, TypeError):
        return None


# — Claude Code session parser —————————————————————————————————————————————


def parse_claude_session(path):
    """Parse a Claude Code JSONL session file, returning (metadata, messages)."""
    session_id = Path(path).stem
    project = None
    slug = None
    earliest_ts = None
    messages = []
    total_input = 0
    total_output = 0
    total_cache_read = 0
    total_cache_create = 0
    # v2 fields (claude-resume picker)
    has_rename = False
    live_title = None  # last custom-title record wins — matches CC's UI
    first_user_msg = None

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                etype = entry.get("type", "")

                # Extract cwd from any entry
                if not project:
                    cwd = entry.get("cwd", "")
                    if cwd:
                        project = cwd

                # Extract slug from any entry
                if not slug:
                    slug = entry.get("slug", "") or entry.get("leafName", "")

                # Parse timestamp
                ts_raw = entry.get("timestamp")
                ts_ms = parse_iso_timestamp(ts_raw)
                if ts_ms and (earliest_ts is None or ts_ms < earliest_ts):
                    earliest_ts = ts_ms

                # v2: /rename gate (user explicitly named this session at least once)
                raw_content = entry.get("content", "")
                if (
                    isinstance(raw_content, str)
                    and "Session renamed to: " in raw_content
                ):
                    has_rename = True

                # v2: custom-title records carry the live title. CC's auto-retitler
                # also writes these as the conversation drifts, so the LAST record
                # wins — it matches what the CC UI shows post-resume.
                if etype == "custom-title":
                    ct = entry.get("customTitle")
                    if isinstance(ct, str) and ct.strip():
                        live_title = ct.strip()

                # Accumulate token usage from assistant messages
                msg_obj = entry.get("message", {})
                if isinstance(msg_obj, dict):
                    usage = msg_obj.get("usage", {})
                    if usage:
                        total_input += usage.get("input_tokens", 0)
                        total_output += usage.get("output_tokens", 0)
                        total_cache_read += usage.get("cache_read_input_tokens", 0)
                        total_cache_create += usage.get(
                            "cache_creation_input_tokens", 0
                        )

                # Determine role: check both "type" and "role" fields
                role = entry.get("role", "")
                if role not in ("user", "assistant"):
                    if etype == "user" or etype == "human":
                        role = "user"
                    elif etype == "assistant":
                        role = "assistant"
                    else:
                        continue

                # Extract text content — handle multiple formats:
                # 1. {message: {content: "..."}} or {message: {content: [{type:"text",...}]}}
                # 2. {content: "..."} or {content: [...]}
                content = msg_obj
                if isinstance(content, dict):
                    content = content.get("content", "")
                elif isinstance(content, str):
                    pass
                else:
                    content = entry.get("content", "")

                text = extract_text(content)
                if text:
                    messages.append((role, text))
                    # v2: first user message preview (disambiguates same-titled sessions)
                    if first_user_msg is None and role == "user":
                        clean = re.sub(r"<[^>]+>", "", text).strip()
                        clean = " ".join(clean.split())[:80]
                        if clean and not clean.startswith(("Caveat:", "[Request")):
                            first_user_msg = clean

    except (OSError, PermissionError) as e:
        print(f"Warning: skipping {path}: {e}", file=sys.stderr)
        return None

    if not slug:
        slug = session_id[:12]

    metadata = {
        "session_id": session_id,
        "source": "claude",
        "file_path": path,
        "project": project or "",
        "slug": slug,
        "timestamp": earliest_ts or 0,
        "input_tokens": total_input,
        "output_tokens": total_output,
        "cache_read_tokens": total_cache_read,
        "cache_create_tokens": total_cache_create,
        "has_rename": 1 if has_rename else 0,
        "live_title": live_title,
        "first_user_msg": first_user_msg,
    }
    return metadata, messages


# — Codex session parser ———————————————————————————————————————————————————


def parse_codex_session(path):
    """Parse a Codex JSONL session file, returning (metadata, messages).

    Codex sessions live in ~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl.
    Supports two formats:
      - Legacy: flat entries with {role, content, record_type, id, ...}
      - Current: wrapped entries with {timestamp, type, payload: {role, content, ...}}
    """
    session_id = Path(path).stem
    project = None
    slug = None
    earliest_ts = None
    messages = []

    # Extract date from path: sessions/YYYY/MM/DD/rollout-...
    path_match = re.search(r"sessions/(\d{4}/\d{2}/\d{2})/", path)
    date_slug = path_match.group(1).replace("/", "-") if path_match else None

    # Extract session UUID from filename: rollout-YYYY-MM-DDTHH-MM-SS-<uuid>.jsonl
    uuid_match = re.search(
        r"-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
        session_id,
    )

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # Skip state snapshots (legacy format)
                if entry.get("record_type") == "state":
                    continue

                # Parse timestamp (present in both formats at top level)
                ts_raw = entry.get("timestamp")
                if ts_raw:
                    ts_ms = parse_iso_timestamp(ts_raw)
                    if ts_ms and (earliest_ts is None or ts_ms < earliest_ts):
                        earliest_ts = ts_ms

                etype = entry.get("type", "")

                # Current format: {type: "session_meta", payload: {id, cwd, ...}}
                if etype == "session_meta":
                    payload = entry.get("payload", {})
                    entry_id = payload.get("id", "")
                    if entry_id and session_id.startswith("rollout-"):
                        session_id = entry_id
                    if not project:
                        project = payload.get("cwd", "")
                    continue

                # Current format: {type: "response_item", payload: {role, content, ...}}
                # Legacy format: {role, content, ...} (no type or type="message")
                if etype == "response_item":
                    payload = entry.get("payload", {})
                    role = payload.get("role", "")
                    content = payload.get("content", "")
                elif etype in ("event_msg", "turn_context"):
                    continue
                else:
                    # Legacy format — session metadata in first entry
                    if not project and "id" in entry and "instructions" in entry:
                        entry_id = entry.get("id", "")
                        if entry_id and session_id.startswith("rollout-"):
                            session_id = entry_id
                        continue

                    role = entry.get("role", "")
                    content = entry.get("content", "")

                    # Legacy: extract cwd from <environment_context> blocks
                    if not project and isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict):
                                text = block.get("text", "")
                                if "Current working directory:" in text:
                                    cwd_match = re.search(
                                        r"Current working directory:\s*(.+)", text
                                    )
                                    if cwd_match:
                                        project = cwd_match.group(1).strip()

                # Only index user and assistant messages (skip developer/system)
                if role not in ("user", "assistant"):
                    continue

                text = extract_text(content)

                # Skip system/instruction blocks injected as user messages
                if not text:
                    continue
                if any(marker in text for marker in CODEX_SKIP_MARKERS):
                    continue

                messages.append((role, text))

    except (OSError, PermissionError) as e:
        print(f"Warning: skipping {path}: {e}", file=sys.stderr)
        return None

    if not slug:
        short_id = uuid_match.group(1)[:8] if uuid_match else session_id[:8]
        slug = f"{date_slug}-{short_id}" if date_slug else short_id

    # v2: first user message preview — Codex has no /rename concept, so
    # has_rename stays 0 and live_title stays NULL. first_user_msg is cheap
    # and keeps the picker's disambiguation code uniform.
    first_user_msg = None
    for role, text in messages:
        if role == "user":
            clean = re.sub(r"<[^>]+>", "", text).strip()
            clean = " ".join(clean.split())[:80]
            if clean and not clean.startswith(("Caveat:", "[Request")):
                first_user_msg = clean
                break

    metadata = {
        "session_id": session_id,
        "source": "codex",
        "file_path": path,
        "project": project or "",
        "slug": slug,
        "timestamp": earliest_ts or 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_create_tokens": 0,
        "has_rename": 0,
        "live_title": None,
        "first_user_msg": first_user_msg,
    }
    return metadata, messages


# — Indexing ———————————————————————————————————————————————————————————————


def _walk_jsonl(root):
    """Yield every *.jsonl path under `root`, NOT following symlinks.

    Mirrors the recall-rs Rust indexer's WalkDir default. Following
    symlinks here silently double-indexed every session reachable via
    the legacy ~/.claude/projects/-Users-markliu -> ./-Users-spidey
    self-loop, which on a 4500-file corpus drove the recall.db file
    to 13 GB+ on disk before the next VACUUM.
    """
    if not root.exists():
        return
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for f in filenames:
            if f.endswith(".jsonl"):
                yield os.path.join(dirpath, f)


def index_sessions(conn, force=False):
    """Scan and index new/changed session files from all sources.

    Steady-state: build a pending list under read-only access first;
    only acquire the writer lock when at least one file has changed.
    Without this fast path every search-driven invocation would briefly
    fight the launchd reindex job (or recall-rs equivalent) for the
    writer lock, returning 'database is locked' before busy_timeout
    can intervene.
    """
    if force:
        conn.executescript("""
            DELETE FROM sessions;
            DELETE FROM messages;
            DELETE FROM messages_cjk;
        """)
        conn.commit()

    # Get existing mtimes keyed by file_path (stable across session_id changes)
    existing = {}
    try:
        for row in conn.execute("SELECT file_path, session_id, mtime FROM sessions"):
            existing[row[0]] = (row[1], row[2])
    except sqlite3.OperationalError:
        pass

    # Collect candidate files from both sources.
    #
    # Skip rules:
    # - subagents/ — subagent transcripts live inside a parent session and
    #   their content already bleeds into the parent's index.
    # - .sync-conflict-* — Syncthing collision artifacts, not real sessions;
    #   they share a session_id with the canonical file and confuse the
    #   claude-resume picker (two rows for the same logical session).
    sources = []
    for fpath in _walk_jsonl(CLAUDE_PROJECTS_DIR):
        if "/subagents/" in fpath or ".sync-conflict-" in fpath:
            continue
        sources.append((fpath, "claude"))
    for fpath in _walk_jsonl(CODEX_SESSIONS_DIR):
        if ".sync-conflict-" in fpath:
            continue
        sources.append((fpath, "codex"))

    # Pre-filter outside any write lock — stat-only.
    pending = []
    skipped = 0
    for fpath, source in sources:
        try:
            mtime = os.path.getmtime(fpath)
        except OSError:
            continue
        if not force and fpath in existing and existing[fpath][1] == mtime:
            skipped += 1
            continue
        pending.append((fpath, source, mtime))

    indexed = 0

    # Fast path: nothing changed — return totals without taking the writer lock.
    # This is the steady-state for a search call against an up-to-date index
    # and is what allows concurrent invocations to coexist with a background
    # indexer (Rust recall-rs or another --index-only Python pass).
    if not pending:
        total_sessions = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        total_messages = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        return indexed, skipped, total_sessions, total_messages

    # Drop previously-indexed rows whose file_path now matches a skip pattern.
    # Done inside the writer-lock window so the fast path above stays read-only.
    for pat in ("/subagents/", ".sync-conflict-"):
        conn.execute("DELETE FROM sessions WHERE file_path LIKE ?", (f"%{pat}%",))

    # Disable FTS5 automerge during bulk insert; restore in a finally so a
    # crash mid-batch can't leave the index permanently un-merged.
    conn.execute("INSERT INTO messages(messages, rank) VALUES('automerge', 0)")
    conn.execute("INSERT INTO messages_cjk(messages_cjk, rank) VALUES('automerge', 0)")
    try:
        for fpath, source, mtime in pending:
            # Remove old data for this file if re-indexing
            if fpath in existing:
                old_sid = existing[fpath][0]
                conn.execute("DELETE FROM sessions WHERE session_id = ?", (old_sid,))
                conn.execute("DELETE FROM messages WHERE session_id = ?", (old_sid,))
                conn.execute(
                    "DELETE FROM messages_cjk WHERE session_id = ?", (old_sid,)
                )

            if source == "claude":
                result = parse_claude_session(fpath)
            else:
                result = parse_codex_session(fpath)

            if result is None:
                continue

            metadata, messages = result

            conn.execute(
                """INSERT OR REPLACE INTO sessions (
                       session_id, source, file_path, project, slug, timestamp, mtime,
                       input_tokens, output_tokens, cache_read_tokens, cache_create_tokens,
                       has_rename, live_title, first_user_msg
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    metadata["session_id"],
                    metadata["source"],
                    metadata["file_path"],
                    metadata["project"],
                    metadata["slug"],
                    metadata["timestamp"],
                    mtime,
                    metadata.get("input_tokens", 0),
                    metadata.get("output_tokens", 0),
                    metadata.get("cache_read_tokens", 0),
                    metadata.get("cache_create_tokens", 0),
                    metadata.get("has_rename", 0),
                    metadata.get("live_title"),
                    metadata.get("first_user_msg"),
                ),
            )

            msg_rows = [(metadata["session_id"], role, text) for role, text in messages]
            conn.executemany(
                "INSERT INTO messages (session_id, role, text) VALUES (?, ?, ?)",
                msg_rows,
            )
            cjk_rows = [r for r in msg_rows if has_cjk(r[2])]
            if cjk_rows:
                conn.executemany(
                    "INSERT INTO messages_cjk (session_id, role, text) VALUES (?, ?, ?)",
                    cjk_rows,
                )

            indexed += 1

        conn.commit()

        if indexed > 0:
            conn.execute("INSERT INTO messages(messages) VALUES('optimize')")
            conn.execute("INSERT INTO messages_cjk(messages_cjk) VALUES('optimize')")
            conn.commit()
    finally:
        conn.execute("INSERT INTO messages(messages, rank) VALUES('automerge', 4)")
        conn.execute(
            "INSERT INTO messages_cjk(messages_cjk, rank) VALUES('automerge', 4)"
        )
        conn.commit()

    # Get totals
    total_sessions = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    total_messages = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]

    return indexed, skipped, total_sessions, total_messages


# — Search —————————————————————————————————————————————————————————————————


def sanitize_fts_query(query):
    """Sanitize a query for FTS5 MATCH.

    FTS5 interprets bare hyphens as the NOT operator, so 'ask-codex' becomes
    'ask NOT codex' which errors out when 'codex' isn't a column name.
    Fix: split hyphenated words into individually quoted segments so
    'ask-codex' -> '"ask" "codex"' (proximity match, no boolean interpretation).
    User-quoted phrases and explicit boolean operators are preserved.
    """
    # Don't touch anything inside double quotes (phrases)
    parts = []
    in_quote = False
    for segment in query.split('"'):
        if in_quote:
            parts.append(f'"{segment}"')
        else:
            # Quote each part of hyphenated words individually
            # e.g. "ask-codex" -> '"ask" "codex"'
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
    # Pick the right FTS table based on query content
    use_cjk = has_cjk(query)
    fts_table = "messages_cjk" if use_cjk else "messages"

    # Trigram requires 3+ char queries. For shorter CJK queries, fall back to LIKE.
    use_like = use_cjk and len(query.strip()) < 3

    # Build session filter (shared by both paths)
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

    # Over-fetch candidates so recency re-ranking can surface recent results
    candidate_limit = limit * 3

    if use_like:
        # LIKE fallback for short CJK queries (< 3 chars)
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
        # FTS5 MATCH path (normal)
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
        # Get session metadata
        meta = conn.execute(
            "SELECT source, file_path, project, slug, timestamp FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if not meta:
            continue

        # Get snippet from the best-matching row
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

        # Apply recency bias: blend BM25 score with a time-decay boost.
        # BM25 rank is negative (more negative = better match).
        # Recency boost: 1.0 for today, decaying with a half-life of 30 days.
        timestamp = meta[4]
        if timestamp:
            age_days = max((now_ms - timestamp) / 86_400_000, 0)
            recency_boost = math.exp(-0.693 * age_days / 30)  # half-life = 30 days
        else:
            recency_boost = 0.0
        # Blend: 80% BM25, 20% recency. Recency term scales with typical BM25 magnitude.
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

    # Re-sort by blended rank and trim to requested limit.
    results.sort(key=lambda r: r[7])
    return results[:limit]


def format_timestamp(ts_ms):
    """Format millisecond timestamp to date string."""
    if not ts_ms:
        return "unknown"
    try:
        ts = float(ts_ms) / 1000  # epoch ms to seconds
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

    # Top 10 sessions by total tokens
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
        print(f"\nTop 10 sessions by token usage:")
        for slug, project, total_tok, inp_tok, out_tok, ts in top:
            date = format_timestamp(ts)
            proj = Path(project).name if project else "?"
            print(
                f"  {date} | {slug:<20} | {proj:<15} | in:{inp_tok:>8,} out:{out_tok:>8,} = {total_tok:>9,}"
            )


def open_db(read_only=False):
    """Open the recall database.

    read_only=True opens via ``file:...?mode=ro`` and skips schema / migrate
    calls so a search-only invocation never contends with the writer lock held
    by a concurrent ``--index-only`` background pass. Returns None if the DB
    doesn't exist yet — caller should fall back to read_only=False to bootstrap.
    """
    migrate_db_location()
    if read_only:
        if not DB_PATH.exists():
            return None
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        # Brief patience for WAL checkpointers; not for write-lock contention.
        conn.execute("PRAGMA busy_timeout=2000")
        return conn
    new_db = not DB_PATH.exists()
    old_umask = os.umask(0o077)
    conn = sqlite3.connect(str(DB_PATH))
    os.umask(old_umask)
    if new_db:
        os.chmod(str(DB_PATH), 0o600)
    # 60s busy_timeout — RW connections can race with the recall-rs Rust
    # indexer (every 5 min via launchd). Without this, any overlap with
    # its writer transaction returns 'database is locked' immediately
    # instead of waiting briefly for the lock to free.
    conn.execute("PRAGMA busy_timeout=60000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    create_schema(conn)
    migrate_schema(conn)
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
        "--reindex", action="store_true", help="Force full rebuild of the index"
    )
    parser.add_argument(
        "--index-only",
        action="store_true",
        help="Reindex without searching (for background jobs)",
    )
    parser.add_argument(
        "--stats", action="store_true", help="Show token usage statistics"
    )

    args = parser.parse_args()

    # Validate query requirement early — before expensive indexing
    if not args.query and not args.stats and not args.index_only:
        parser.error("query is required (or use --index-only / --stats)")

    # Index passes need the write lock; pure searches and stats don't.
    # Opening RO lets a search succeed even while the launchd --index-only
    # job is mid-pass holding the writer lock.
    is_indexing = args.reindex or args.index_only
    conn = open_db(read_only=not is_indexing)
    if conn is None:
        # First run: no DB on disk yet — bootstrap by opening RW and indexing.
        conn = open_db(read_only=False)
        is_indexing = True

    if is_indexing:
        t0 = time.time()
        indexed, skipped, total_sessions, total_messages = index_sessions(
            conn, force=args.reindex
        )
        index_time = time.time() - t0
        if indexed > 0:
            print(f"Indexed {indexed} sessions in {index_time:.1f}s", file=sys.stderr)
    else:
        # Read-only path: cheap headline counts only. Skip the per-message
        # COUNT(*) on the FTS table — it's only used in the display banner.
        total_sessions = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        total_messages = None

    # Stats mode
    if args.stats:
        show_stats(conn, days=args.days, source=args.source)
        conn.close()
        return

    # Index-only mode
    if args.index_only:
        print(
            f"Index: {total_sessions} sessions, {total_messages} messages",
            file=sys.stderr,
        )
        conn.close()
        return

    # Search
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

    if total_messages is None:
        print(f"Found {len(results)} sessions (index: {total_sessions} sessions):\n")
    else:
        print(
            f"Found {len(results)} sessions (index: {total_sessions} sessions, {total_messages} messages):\n"
        )

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
            # Clean up excerpt for display
            excerpt_clean = excerpt.replace("\n", " ").strip()
            if len(excerpt_clean) > 200:
                excerpt_clean = excerpt_clean[:200] + "..."
            print(f"    > {excerpt_clean}")
        print()

    conn.close()


if __name__ == "__main__":
    main()
