"""
MEET_RUDI platform — connection details for the platform database (PlanetScale Vitess, MySQL 8;
decision #46).

The credentials live in Secrets Manager (meetrudi/planetscale-backbone/…). This module only reads
a secret's text; fetching it and opening the connection happen in the caller, so nothing here
needs boto3 or a driver.

Accepted secret shapes:
  - a URL: mysql://user:password@host:3306/database (PlanetScale's "Connect" string; its query
    part, e.g. ?ssl={...}, is ignored: TLS is always on)
  - JSON with a URL under url, uri, dsn, connection_string or database_url
  - JSON with fields: host, port, database (or dbname), user (or username), password

Key names are matched without case, '-' or '_' (`user-name` is `username`). What the secret does
not say (host, port, database) comes from `defaults`, the caller's deployment settings: a host
name is not a credential, so a secret may hold only the user and the password. On PlanetScale a
password belongs to one branch, and the database name is the PlanetScale database's name.

TLS is always verified, whatever the secret says: the database is outside our AWS account, so an
unverified connection could hand the credentials to anyone on the path.

Errors name the keys a secret has, never its values.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote, urlsplit

DEFAULT_PORT = 3306
URL_KEYS = ("url", "uri", "dsn", "connectionstring", "databaseurl")
HOST_KEYS = ("host", "hostname", "server")
USER_KEYS = ("user", "username")
PASSWORD_KEYS = ("password", "pass", "pwd")
DATABASE_KEYS = ("database", "dbname", "db")
EU_REGION_RE = re.compile(r"eu-(?:central|west|north|south)-\d")


@dataclass(frozen=True)
class ConnectionInfo:
    host: str
    port: int
    database: str
    user: str
    password: str = field(repr=False)

    def describe(self) -> dict[str, Any]:
        """What may be shown or logged: no password, no full host."""
        labels = self.host.split(".")
        region = EU_REGION_RE.search(self.host)
        return {
            "host_domain": ".".join(labels[-3:]) if len(labels) > 3 else self.host,
            "region_in_host": region.group(0) if region else None,
            "port": self.port,
            "database": self.database,
        }


def _first(data: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for k in keys:
        if data.get(k) not in (None, ""):
            return data[k]
    return None


def _from_url(url: str, defaults: Mapping[str, Any]) -> ConnectionInfo:
    parts = urlsplit(url.strip())
    if parts.scheme != "mysql":
        raise ValueError("the connection URL must start with mysql://")
    if not parts.hostname or not parts.username or parts.password is None:
        raise ValueError("the connection URL needs a user, a password and a host")
    database = unquote(parts.path.lstrip("/")) or defaults.get("database")
    if not database:
        raise ValueError("the connection URL names no database")
    return ConnectionInfo(
        host=parts.hostname,
        port=parts.port or DEFAULT_PORT,
        database=str(database),
        user=unquote(parts.username),
        password=unquote(parts.password),
    )


def _norm(key: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def parse_secret(
    text: str, defaults: Mapping[str, Any] | None = None
) -> ConnectionInfo:
    """Connection details from a secret's text, in any of the shapes above.

    `defaults` may give host, port and database; the secret's own values win.
    """
    defaults = {k: v for k, v in (defaults or {}).items() if v not in (None, "")}
    text = (text or "").strip()
    if text.startswith("mysql://"):
        return _from_url(text, defaults)
    try:
        raw = json.loads(text)
    except ValueError:
        raise ValueError("the secret is neither JSON nor a mysql:// URL") from None
    if not isinstance(raw, dict):
        raise ValueError("the secret's JSON must be an object")  # noqa: TRY004 - a bad secret, like the rest
    data = {_norm(k): v for k, v in raw.items()}
    url = _first(data, URL_KEYS)
    if isinstance(url, str) and url.strip():
        return _from_url(url, defaults)
    host = _first(data, HOST_KEYS) or defaults.get("host")
    user = _first(data, USER_KEYS)
    password = _first(data, PASSWORD_KEYS)
    database = _first(data, DATABASE_KEYS) or defaults.get("database")
    missing = [
        what
        for what, value in (
            ("host", host),
            ("user", user),
            ("password", password),
            ("database", database),
        )
        if value is None
    ]
    if missing:
        keys = ", ".join(sorted(str(k).strip() for k in raw))
        raise ValueError(f"the secret has no {', '.join(missing)} (its keys: {keys})")
    port = _first(data, ("port",)) or defaults.get("port") or DEFAULT_PORT
    try:
        port = int(port)
    except (TypeError, ValueError):
        raise ValueError("the port is not a number") from None
    return ConnectionInfo(
        host=str(host).strip(),
        port=port,
        database=str(database).strip(),
        user=str(user).strip(),
        password=str(password),
    )
