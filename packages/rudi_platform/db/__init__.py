"""
MEET_RUDI platform — the platform database (PlanetScale Vitess, MySQL 8; decision #46).

    connect       reading a connection secret, never fetching it
    tenant_guard  TenantBound: the only way application code reaches PlanetScale; refuses,
                  before sending, any statement on a tenant's rows that does not name the
                  tenant (decision #47)
    migrate       finding, checking and applying db/migrations (the one process without the
                  guard: it changes the schema and writes no tenant's rows)

Standard library only: the driver (PyMySQL) and boto3 live in the services that connect.
"""

from .connect import ConnectionInfo, parse_secret
from .migrate import (
    Migration,
    MigrationError,
    MigrationFailed,
    Plan,
    apply,
    load,
    plan,
    read_applied,
    server_report,
    split_statements,
)
from .tenant_guard import (
    RESOLVERS,
    TenantBound,
    UnscopedStatement,
    check_statement,
    resolve,
)

__all__ = [
    "RESOLVERS",
    "ConnectionInfo",
    "Migration",
    "MigrationError",
    "MigrationFailed",
    "Plan",
    "TenantBound",
    "UnscopedStatement",
    "apply",
    "check_statement",
    "load",
    "parse_secret",
    "plan",
    "read_applied",
    "resolve",
    "server_report",
    "split_statements",
]
