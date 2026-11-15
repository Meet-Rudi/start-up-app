#!/usr/bin/env python3
"""
Prepare a migration for the PlanetScale web console, where the user applies migrations
(decision #44).

    python db/console_sql.py 2                  writes db/console/0002_seed_access.sql
    python db/console_sql.py 1 --record-only    only the record that 0001 is applied

The file holds the migration's statements, each ending in `;`, with every {new_id:cat} replaced
by a fresh id (the console cannot mint our ids, and MySQL on PlanetScale has no stored functions
to do it). It ends with two statements that record the migration in meta_migrations, creating
that table the first time, so the database itself says what is applied and
`python db/migrate.py plan` stays right.

Ids are minted when the file is written: apply a file once, and generate it again if you need it
again. db/console/ is git-ignored. Nothing here connects to PlanetScale.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "packages"))

from rudi_platform.db.migrate import MigrationError, console_script, load

MIGRATIONS = os.path.join(HERE, "migrations")
OUT_DIR = os.path.join(HERE, "console")


def main() -> None:
    args = sys.argv[1:]
    record_only = "--record-only" in args
    args = [a for a in args if a != "--record-only"]
    if len(args) != 1 or not args[0].isdigit():
        print(__doc__.strip().split("\n\n")[1])
        sys.exit(2)
    try:
        migrations = load(MIGRATIONS)
    except MigrationError as e:
        sys.exit(f"!! the migrations do not load: {e}")
    found = [m for m in migrations if m.number == int(args[0])]
    if not found:
        sys.exit(f"!! no migration {int(args[0]):04d} in db/migrations")
    m = found[0]
    os.makedirs(OUT_DIR, exist_ok=True)
    suffix = ".record.sql" if record_only else ".sql"
    path = os.path.join(OUT_DIR, m.name + suffix)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(console_script(m, record_only=record_only))
    what = "the record of" if record_only else f"{len(m.statements)} statements + the record of"
    print(f"Written: {os.path.relpath(path, os.path.join(HERE, '..'))} ({what} {m.name})")


if __name__ == "__main__":
    main()
