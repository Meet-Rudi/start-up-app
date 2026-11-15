#!/usr/bin/env python3
"""
Run the platform database migrations: one command per step (decision #42).

    python db/migrate.py check      connect with each PlanetScale secret (reads only)
    python db/migrate.py plan       what is applied, unfinished, pending, any drift (reads only)
    python db/migrate.py apply      apply the pending migrations, resuming an unfinished one
    python db/migrate.py apply 3    apply up to migration 0003

It invokes the Lambda meetrudi-internal-prod-db-migrate with the rudi-deployer profile: the
database credentials stay in Secrets Manager, which only the Lambda's role can read. Deploy it
first with `python deploy.py db-migrate` (and again after adding a migration: the Lambda applies
the migrations it was deployed with).

The schema is changed by a person, never by an agent (CLAUDE.md §7): `apply` asks you to type
APPLY and refuses to run without an interactive terminal.

Exit code 0 when the step succeeded, 1 when it did not.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

FUNCTION = "meetrudi-internal-prod-db-migrate"
REGION = "eu-central-1"
PROFILE = "rudi-deployer"
ACTIONS = ("check", "plan", "apply")


def _usage() -> None:
    print(__doc__.strip().split("\n\n")[1])
    sys.exit(2)


def _confirm_apply(upto) -> None:
    """Only a person at a terminal may change the schema."""
    refused = (
        "!! apply changes the PlanetScale schema and runs only in an interactive terminal,\n"
        "   typed by a person (CLAUDE.md, section 7). Nothing was sent."
    )
    # Both ends must be a console: on Windows the NUL device also claims to be a terminal.
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print(refused)
        sys.exit(1)
    scope = f"up to and including {upto:04d}" if upto else "every pending migration"
    print(f"This applies {scope} to PlanetScale, statement by statement, each migration")
    print("with its record in meta_migrations. MySQL cannot roll a schema change back: a failed")
    print("statement stops the run, what ran before it stays, and the next apply resumes there.")
    print("Run `python db/migrate.py plan` first if you have not.")
    try:
        answer = input("Type APPLY to continue: ").strip()
    except EOFError:
        answer = ""
    if answer != "APPLY":
        print("\nNot applied. Nothing was sent.")
        sys.exit(1)


def main() -> None:
    args = sys.argv[1:]
    if not args or args[0] not in ACTIONS or len(args) > 2:
        _usage()
    event = {"action": args[0]}
    if args[0] == "apply":
        event["confirm"] = "apply"
        if len(args) == 2:
            if not args[1].isdigit():
                _usage()
            event["upto"] = int(args[1])
        _confirm_apply(event.get("upto"))
    elif len(args) == 2:
        _usage()

    out = os.path.join(tempfile.mkdtemp(), "result.json")
    cmd = [
        shutil.which("aws") or "aws", "lambda", "invoke",
        "--function-name", FUNCTION,
        "--payload", json.dumps(event),
        "--cli-binary-format", "raw-in-base64-out",
        "--cli-read-timeout", "320",
        "--region", REGION,
        "--profile", PROFILE,
        out,
    ]
    print(f"> {FUNCTION} {json.dumps(event)}")
    done = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if done.returncode != 0:
        print("!! the invoke failed:", (done.stderr or done.stdout).strip())
        print("   Is the runner deployed?  python deploy.py db-migrate")
        sys.exit(1)
    with open(out, encoding="utf-8") as fh:
        result = json.load(fh)
    print(json.dumps(result, indent=2))
    sys.exit(0 if isinstance(result, dict) and result.get("ok") else 1)


if __name__ == "__main__":
    main()
