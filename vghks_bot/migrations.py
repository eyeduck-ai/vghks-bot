"""Transactional, resumable additions to the portable account database."""
from collections.abc import Callable


def migrate(db, name: str, apply: Callable):
    # A migration marker and its data changes always commit together. Existing
    # portable format identifiers and original clinical tables remain intact.
    if not db.in_transaction:
        db.execute("BEGIN IMMEDIATE")
    db.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY)")
    if db.execute("SELECT 1 FROM schema_migrations WHERE name=?", (name,)).fetchone():
        return
    apply(db)
    db.execute("INSERT INTO schema_migrations VALUES(?)", (name,))


def statements(db, *sql):
    for statement in sql:
        db.execute(statement)
