"""
MEET_RUDI platform — the tenant guard: no statement on a tenant's rows reaches PlanetScale unless
it names that tenant (decision #47, conventions §8).

PlanetScale Vitess has no row-level security, so tenants are kept apart by the application, and
this module is where that is enforced. Every connection the platform opens to PlanetScale is
wrapped in TenantBound (one tenant, or the platform's own rows) before any other code may use it.
The wrapper checks each statement *before* sending it and raises UnscopedStatement when the
statement does not name the tenant; a refused statement never reaches the database.

What a statement must do, on any table not listed in NO_TENANT_DATA:

  SELECT  every tenant table it reads has `<alias>.tenant_id = %s` as a top-level AND of the
          WHERE clause of its own query level, or, for a joined table, of its own JOIN's ON
          clause. A bare `tenant_id = %s` is enough when the statement reads a single table.
  UPDATE  one table; the same condition in WHERE; never sets tenant_id.
  DELETE  one table; the same condition in WHERE.
  INSERT  lists tenant_id among its columns, with %s for it in every row. ON DUPLICATE KEY
          UPDATE only on tables whose unique keys all lead with tenant_id, never on tenant_id.

The %s bound to each of those conditions must be the guard's own tenant id, so a statement names
*its* tenant, not merely a tenant. The platform's own rows (tenant_id NULL: platform alerts,
jobs, audit events, platform-wide roles) are reached through a platform guard, with
`tenant_id IS NULL` in place of `= %s`. Platform work across tenants runs tenant by tenant.

To keep the check exact, the guard reads a deliberately plain dialect: no comments, no `;`, no
quoted identifiers, no UNION or WITH, no RIGHT, FULL, CROSS or NATURAL joins and no comma joins,
no OR, XOR, BETWEEN or CASE at the top of a WHERE or ON clause (inside parentheses they are
fine), and parameters only as positional %s. A statement it cannot read is refused, never waved
through.

The only statements that may read a tenant table without naming its tenant are the RESOLVERS:
the closed list of lookups that find the tenant of a request arriving without one (an inbound
message names only the address it reached; an API key, an invitation link and a sign-in name no
tenant). They return ids, never personal data, and run only through resolve(). Adding one is a
decision, recorded under #47.

The migration runner (migrate.py) is the one process that connects without this guard: it changes
the schema and seeds platform tables, and tests/test_schema.py checks that no migration writes a
tenant table.

Standard library only. Error messages name tables and reasons, never parameter values.
"""

from __future__ import annotations

import itertools
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from ..ids import ID_RE
from .migrate import Database

# Tables holding no tenant's data, so no tenant_id (decision #41). Every other table, including
# one added tomorrow, is a tenant's: the guard fails closed. Equal to NO_TENANT_DATA in
# tests/test_schema.py, which checks the list against the migrations.
NO_TENANT_DATA = frozenset(
    {
        "registry_cells",
        "registry_tenants",
        "registry_skills",
        "registry_skill_versions",
        "registry_skill_packs",
        "access_control_accounts",
        "access_control_sessions",
        "access_control_permissions",
        "access_control_roles",
        "access_control_role_permissions",
    }
)

# Tenant tables with a unique key that does not lead with tenant_id (the resolvers' keys). An
# INSERT … ON DUPLICATE KEY UPDATE there could update another tenant's row, so it is refused.
GLOBAL_KEY_TABLES = frozenset(
    {
        "registry_endpoints",
        "registry_api_keys",
        "access_control_invitations",
        "access_control_role_assignments",
    }
)


@dataclass(frozen=True)
class Resolver:
    sql: str
    params: tuple[str, ...]  # what the caller passes, in order
    key: str  # the global unique key it reads (GLOBAL_KEYS in tests/test_schema.py)


RESOLVERS: dict[str, Resolver] = {
    # An inbound message names only the business address it reached.
    "endpoint_by_address": Resolver(
        "SELECT tenant_id, clone_id, id, status FROM registry_endpoints "
        "WHERE channel = %s AND address = %s",
        ("channel", "address"),
        "registry_endpoints.endpoints_address_uq",
    ),
    # An API call carries only its key; the store hashes it first.
    "api_key_by_hash": Resolver(
        "SELECT tenant_id, clone_id, id, stage, revoked_at FROM registry_api_keys "
        "WHERE key_hash = %s",
        ("key_hash",),
        "registry_api_keys.api_keys_hash_uq",
    ),
    # An invitation link carries only its token; the rest is read with the tenant's guard.
    "invitation_by_token": Resolver(
        "SELECT tenant_id, id, role_id, expires_at, accepted_at FROM access_control_invitations "
        "WHERE token_hash = %s",
        ("token_hash",),
        "access_control_invitations.invitations_token_uq",
    ),
    # At sign-in, the tenants an account may work in (tenant_id NULL: a platform role).
    "assignments_of_account": Resolver(
        "SELECT tenant_id, role_id, id FROM access_control_role_assignments WHERE account_id = %s",
        ("account_id",),
        "access_control_role_assignments.role_assignments_uq",
    ),
}


class UnscopedStatement(PermissionError):
    """The statement does not name its tenant as the guard requires. It was not sent."""


# ------------------------------------------------------------------------------------- reading
_VERBS = {"SELECT", "INSERT", "UPDATE", "DELETE"}
_REFUSED_WORDS = {
    "UNION": "UNION (run one statement per part)",
    "INTERSECT": "INTERSECT",
    "EXCEPT": "EXCEPT",
    "WITH": "WITH (common table expressions)",
    "RIGHT": "a RIGHT join (write it as a LEFT join)",
    "FULL": "a FULL join",
    "CROSS": "a CROSS join",
    "NATURAL": "a NATURAL join",
    "STRAIGHT_JOIN": "STRAIGHT_JOIN",
    "USING": "JOIN … USING (write ON)",
    "OUTFILE": "INTO OUTFILE",
    "DUMPFILE": "INTO DUMPFILE",
    "LOAD_FILE": "LOAD_FILE",
}
_CLAUSE_WORDS = {
    "ON", "WHERE", "JOIN", "INNER", "LEFT", "OUTER", "GROUP", "ORDER", "LIMIT", "HAVING", "FOR",
    "SET", "LOCK", "WINDOW", "VALUES", "AS",
}  # fmt: skip
_WHERE_END = {"GROUP", "ORDER", "LIMIT", "HAVING", "FOR", "LOCK", "WINDOW"}
_ON_END = _WHERE_END | {"JOIN", "INNER", "LEFT", "WHERE"}
# At the top of a WHERE or ON clause these would let an exact `tenant_id = %s` segment sit
# outside the conjunction (OR, XOR) or inside another expression (BETWEEN … AND, CASE … END).
_TOP_LEVEL_REFUSED = {"OR", "XOR", "BETWEEN", "CASE"}

_TOKEN_RE = re.compile(
    r"\s*(?:"
    r"(?P<str>'(?:[^']|'')*')"
    r"|(?P<param>%s)"
    r"|(?P<word>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?)"
    r"|(?P<num>\d+(?:\.\d+)?)"
    r"|(?P<op><=>|<=|>=|<>|!=|->>|->|[=<>(),*+\-/.!])"  # no ':' — MySQL's := binds below AND
    r")"
)


@dataclass(frozen=True)
class _Tok:
    kind: str  # str | param | word | num | op
    text: str
    up: str  # upper case, for keywords
    depth: int  # parenthesis depth the token sits at
    scope: int  # the query level (top statement 0, each subquery its own)

    @property
    def column(self) -> str:
        """The column part of a (qualified) word, lower case."""
        return self.text.rsplit(".", 1)[-1].lower() if self.kind == "word" else ""

    @property
    def qualifier(self) -> str | None:
        return self.text.rsplit(".", 1)[0] if self.kind == "word" and "." in self.text else None


@dataclass(frozen=True)
class _Ref:
    table: str
    qualifier: str  # alias, or the table name
    scope: int
    joined: bool  # introduced by JOIN (its own ON clause may carry the condition)
    on: tuple[int, ...]  # token indices of its ON clause; empty if none


def _refuse(why: str) -> UnscopedStatement:
    return UnscopedStatement(f"refused before sending: {why} (decision #47)")


def _tokens(sql: str) -> list[_Tok]:
    if not isinstance(sql, str) or not sql.strip():
        raise _refuse("an empty statement")
    if "\\" in sql:
        raise _refuse("a backslash")
    if re.search(r"%(?!s)", sql):
        raise _refuse("a % other than the %s placeholder (pass LIKE patterns as parameters)")
    raw: list[tuple[str, str]] = []
    pos, n = 0, len(sql)
    while pos < n:
        if sql[pos:].strip() == "":
            break
        found = _TOKEN_RE.match(sql, pos)
        if not found or found.end() == pos or found.lastgroup is None:
            rest = sql[pos:].lstrip()[:1]
            what = {
                "-": "a comment", "#": "a comment", "/": "a comment", ";": "more than one statement",
                "`": "a quoted identifier", '"': "a double-quoted name or string",
                "?": "a ? placeholder (use %s)",
            }.get(rest, f"a character it does not read ({rest!r})")  # fmt: skip
            raise _refuse(what)
        text = found.group(found.lastgroup)
        if found.lastgroup == "op" and text == "-" and sql.startswith("--", found.start(found.lastgroup)):
            raise _refuse("a comment")
        if found.lastgroup == "op" and text == "/" and sql.startswith("/*", found.start(found.lastgroup)):
            raise _refuse("a comment")
        raw.append((found.lastgroup, text))
        pos = found.end()

    toks: list[_Tok] = []
    depth = 0
    scopes = [(0, 0)]  # (depth of the level's own tokens, scope id)
    next_scope = 1
    for i, (kind, text) in enumerate(raw):
        up = text.upper()
        if text == "(":
            toks.append(_Tok(kind, text, up, depth, scopes[-1][1]))
            depth += 1
            if i + 1 < len(raw) and raw[i + 1][1].upper() == "SELECT":
                scopes.append((depth, next_scope))
                next_scope += 1
        elif text == ")":
            if len(scopes) > 1 and scopes[-1][0] == depth:
                scopes.pop()
            depth -= 1
            if depth < 0:
                raise _refuse("unbalanced parentheses")
            toks.append(_Tok(kind, text, up, depth, scopes[-1][1]))
        else:
            toks.append(_Tok(kind, text, up, depth, scopes[-1][1]))
    if depth != 0:
        raise _refuse("unbalanced parentheses")
    return toks


class _Statement:
    def __init__(self, sql: str) -> None:
        self.toks = _tokens(sql)
        self.verb = self.toks[0].up
        if self.verb not in _VERBS:
            raise _refuse(f"{self.toks[0].text[:20]} statements: only SELECT, INSERT, UPDATE and DELETE")
        for t in self.toks:
            if t.kind == "word" and t.up in _REFUSED_WORDS:
                raise _refuse(_REFUSED_WORDS[t.up])
            if t.kind == "word" and t.up == "INTO" and self.verb != "INSERT":
                raise _refuse("SELECT … INTO")
            if t.kind == "op" and t.text in ("||", "&&"):
                raise _refuse(f"{t.text} (write OR or AND)")
        # Where each level's own tokens sit.
        self.home: dict[int, int] = {}
        for t in self.toks:
            self.home.setdefault(t.scope, t.depth)
        self.params = [i for i, t in enumerate(self.toks) if t.kind == "param"]

    def at_home(self, i: int) -> bool:
        t = self.toks[i]
        return t.depth == self.home[t.scope]

    def clause(self, start: int, ends: set[str]) -> tuple[int, ...]:
        """Token indices after `start` up to the clause's end, at start's level."""
        scope, home = self.toks[start].scope, self.toks[start].depth
        out = []
        for i in range(start + 1, len(self.toks)):
            t = self.toks[i]
            if t.depth < home or (t.scope == scope and t.depth == home and t.up in ends):
                break
            out.append(i)
        return tuple(out)

    def conjuncts(self, indices: tuple[int, ...], home: int) -> list[list[int]]:
        """Split a clause of a level whose own tokens sit at depth `home` on its top-level AND;
        refuse what would make that split inexact."""
        parts: list[list[int]] = [[]]
        for i in indices:
            t = self.toks[i]
            if t.depth == home and t.kind == "word" and t.up in _TOP_LEVEL_REFUSED:
                raise _refuse(
                    f"{t.up} at the top of a WHERE or ON clause (put it in parentheses, after "
                    "the tenant condition)"
                )
            if t.depth == home and t.kind == "word" and t.up == "AND":
                parts.append([])
            else:
                parts[-1].append(i)
        return parts

    def table_at(self, i: int) -> tuple[str, str | None, int]:
        """(table, alias, index after them) for a table name at token i."""
        t = self.toks[i] if i < len(self.toks) else None
        if t is None or t.kind != "word" or t.up in _CLAUSE_WORDS:
            raise _refuse("a statement whose table it cannot find")
        if "." in t.text:
            raise _refuse("a database-qualified table name")
        j, alias = i + 1, None
        if j < len(self.toks) and self.toks[j].up == "AS":
            if j + 1 >= len(self.toks) or self.toks[j + 1].kind != "word":
                raise _refuse("AS without an alias")
            alias, j = self.toks[j + 1].text, j + 2
        elif (
            j < len(self.toks)
            and self.toks[j].kind == "word"
            and self.toks[j].up not in _CLAUSE_WORDS
            and self.toks[j].scope == t.scope
        ):
            alias, j = self.toks[j].text, j + 1
        if alias is not None and "." in alias:
            raise _refuse("a qualified alias")
        return t.text, alias, j

    def refs(self) -> list[_Ref]:
        """Every table a SELECT, UPDATE or DELETE reads or writes, by query level."""
        out: list[_Ref] = []
        toks = self.toks
        start = 0
        if self.verb == "UPDATE":
            table, alias, j = self.table_at(1)
            out.append(_Ref(table, alias or table, 0, False, ()))
            if j >= len(toks) or toks[j].up != "SET":
                raise _refuse("UPDATE of more than one table")
            start = j
        if self.verb == "DELETE" and (len(toks) < 2 or toks[1].up != "FROM"):
            raise _refuse("DELETE of more than one table, or with modifiers")
        for i in range(start, len(toks)):
            t = toks[i]
            if t.kind != "word" or t.up not in ("FROM", "JOIN") or not self.at_home(i):
                continue
            if i + 1 < len(toks) and toks[i + 1].text == "(":
                continue  # a derived table: its own level is checked on its own
            table, alias, j = self.table_at(i + 1)
            if j < len(toks) and toks[j].text == "," and toks[j].scope == t.scope and self.at_home(j):
                raise _refuse("a comma join (write JOIN … ON)")
            on: tuple[int, ...] = ()
            if t.up == "JOIN" and j < len(toks) and toks[j].up == "ON":
                on = self.clause(j, _ON_END)
            out.append(_Ref(table, alias or table, t.scope, t.up == "JOIN", on))
        if self.verb in ("UPDATE", "DELETE"):
            top = [r for r in out if r.scope == 0]
            if len(top) != 1:
                raise _refuse(f"{self.verb} of more than one table")
        return out

    def where(self, scope: int) -> tuple[int, ...] | None:
        for i, t in enumerate(self.toks):
            if t.scope == scope and t.up == "WHERE" and self.at_home(i):
                return self.clause(i, _WHERE_END)
        return None


def _is_tenant_table(table: str) -> bool:
    return table.lower() not in NO_TENANT_DATA


# ------------------------------------------------------------------------------------ checking
def _bound(st: _Statement, i: int, params: Sequence[Any]) -> Any:
    return params[st.params.index(i)]


def _names_tenant(
    st: _Statement, part: list[int], qualifiers: set[str | None], tenant_id: str | None,
    params: Sequence[Any],
) -> bool:
    """Is this conjunct exactly the tenant condition for one of `qualifiers`?"""
    toks = [st.toks[i] for i in part]
    if not toks or toks[0].column != "tenant_id" or toks[0].qualifier not in qualifiers:
        return False
    if tenant_id is None:
        return len(toks) == 3 and toks[1].up == "IS" and toks[2].up == "NULL"
    if len(toks) == 3 and toks[1].text == "=" and toks[2].kind == "param":
        return _bound(st, part[2], params) == tenant_id
    return False


def _check_reads(st: _Statement, tenant_id: str | None, params: Sequence[Any]) -> None:
    refs = st.refs()
    single = len(refs) == 1
    for ref in refs:
        if not _is_tenant_table(ref.table):
            continue
        qualifiers: set[str | None] = {ref.qualifier}
        if single:
            qualifiers.add(None)  # a bare tenant_id is unambiguous with one table
        clauses = []
        where = st.where(ref.scope)
        if where is not None:
            clauses.append(where)
        if ref.joined and ref.on:
            clauses.append(ref.on)
        if not any(
            _names_tenant(st, part, qualifiers, tenant_id, params)
            for clause in clauses
            for part in st.conjuncts(clause, st.home[ref.scope])
        ):
            wanted = "IS NULL" if tenant_id is None else "= %s, bound to this guard's tenant"
            where_to = "its JOIN's ON clause or the WHERE clause" if ref.joined else "the WHERE clause"
            raise _refuse(
                f"{st.verb} on {ref.table} without `{ref.qualifier}.tenant_id {wanted}` as a "
                f"top-level AND of {where_to} of its query level"
            )
    if st.verb == "UPDATE":
        set_at = next(i for i, t in enumerate(st.toks) if t.up == "SET" and t.scope == 0)
        assignments = st.clause(set_at, {"WHERE", "ORDER", "LIMIT"})
        _no_tenant_assignment(st, assignments, "UPDATE … SET")


def _no_tenant_assignment(st: _Statement, indices: tuple[int, ...], where: str) -> None:
    for a, b in itertools.pairwise(indices):
        if st.toks[a].column == "tenant_id" and st.toks[b].text == "=" and st.at_home(a):
            raise _refuse(f"{where} assigns tenant_id: rows never move between tenants")


def _split_top(st: _Statement, open_at: int) -> tuple[list[list[int]], int]:
    """The comma-separated items inside the parentheses opening at `open_at`, and the index
    after the closing parenthesis."""
    depth = st.toks[open_at].depth + 1
    items: list[list[int]] = [[]]
    i = open_at + 1
    while i < len(st.toks):
        t = st.toks[i]
        if t.text == ")" and t.depth == depth - 1:
            return items, i + 1
        if t.text == "," and t.depth == depth:
            items.append([])
        else:
            items[-1].append(i)
        i += 1
    raise _refuse("unbalanced parentheses")


def _check_insert(st: _Statement, tenant_id: str | None, params: Sequence[Any]) -> None:
    toks = st.toks
    if len(toks) < 3 or toks[1].up != "INTO":
        raise _refuse("INSERT with modifiers (write INSERT INTO)")
    if any(t.up == "SELECT" for t in toks):
        raise _refuse("INSERT … SELECT (read with the guard, then insert values)")
    table = toks[2].text
    if toks[2].kind != "word" or "." in table:
        raise _refuse("a statement whose table it cannot find")
    if len(toks) < 4 or toks[3].text != "(":
        raise _refuse("INSERT without a column list")
    columns, j = _split_top(st, 3)
    names = []
    for col in columns:
        if len(col) != 1 or toks[col[0]].kind != "word":
            raise _refuse("a column list it cannot read")
        names.append(toks[col[0]].column)
    if j >= len(toks) or toks[j].up != "VALUES":
        raise _refuse("INSERT without VALUES")
    j += 1
    tenant = _is_tenant_table(table)
    if tenant and "tenant_id" not in names:
        raise _refuse(f"INSERT into {table} without a tenant_id column")
    rows = 0
    while j < len(toks) and toks[j].text == "(":
        values, j = _split_top(st, j)
        if len(values) != len(names):
            raise _refuse("a row whose values do not match its columns")
        if tenant:
            value = values[names.index("tenant_id")]
            ok = False
            if len(value) == 1:
                v = toks[value[0]]
                if v.kind == "param":
                    ok = _bound(st, value[0], params) == tenant_id
                elif v.up == "NULL":
                    ok = tenant_id is None
            if not ok:
                wanted = "NULL" if tenant_id is None else "%s bound to this guard's tenant"
                raise _refuse(f"INSERT into {table} whose tenant_id is not {wanted}")
        rows += 1
        if j < len(toks) and toks[j].text == ",":
            j += 1
    if rows == 0:
        raise _refuse("INSERT without rows")
    if j < len(toks) and toks[j].up == "AS":
        j += 2  # the row alias of ON DUPLICATE KEY UPDATE
    if j == len(toks):
        return
    if [t.up for t in toks[j : j + 4]] != ["ON", "DUPLICATE", "KEY", "UPDATE"]:
        raise _refuse("something after the VALUES it does not read")
    if tenant and table.lower() in GLOBAL_KEY_TABLES:
        raise _refuse(
            f"ON DUPLICATE KEY UPDATE on {table}: a unique key there does not lead with "
            "tenant_id, so it could update another tenant's row"
        )
    _no_tenant_assignment(st, tuple(range(j + 4, len(toks))), "ON DUPLICATE KEY UPDATE")


def check_statement(sql: str, params: Sequence[Any], tenant_id: str | None) -> None:
    """Raise UnscopedStatement unless `sql` names `tenant_id` as this module requires.

    `tenant_id` None means the platform's own rows (tenant_id IS NULL).
    """
    if isinstance(params, (str, bytes)) or not isinstance(params, Sequence):
        raise _refuse("parameters that are not a sequence for positional %s")
    st = _Statement(sql)
    if len(st.params) != len(params):
        raise _refuse(f"{len(st.params)} placeholders for {len(params)} parameters")
    if st.verb == "INSERT":
        _check_insert(st, tenant_id, params)
    else:
        _check_reads(st, tenant_id, params)


# ---------------------------------------------------------------------------------- connection
def _valid_tenant(tenant_id: str) -> str:
    found = ID_RE.match(tenant_id or "")
    if not found or found.group("cat") != "ten":
        raise ValueError("a tenant guard needs a ten_ id")
    return tenant_id


class TenantBound:
    """A PlanetScale connection that sends only statements naming its tenant.

    TenantBound(db, "ten_…")  a tenant's rows
    TenantBound.platform(db)  the platform's own rows (tenant_id NULL) and tables of no tenant
    """

    def __init__(self, db: Database, tenant_id: str, *, _platform: bool = False) -> None:
        self._db = db
        self.tenant_id: str | None = None if _platform else _valid_tenant(tenant_id)

    @classmethod
    def platform(cls, db: Database) -> TenantBound:
        return cls(db, "", _platform=True)

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
        check_statement(sql, params, self.tenant_id)
        return self._db.query(sql, params)

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        check_statement(sql, params, self.tenant_id)
        return self._db.execute(sql, params)


def resolve(db: Database, resolver: str, params: Sequence[Any]) -> list[tuple[Any, ...]]:
    """Run one of the RESOLVERS: the only unscoped reads of a tenant table."""
    found = RESOLVERS.get(resolver)
    if found is None:
        raise _refuse(f"{resolver!r} is not one of the resolvers")
    if isinstance(params, (str, bytes)) or len(params) != len(found.params):
        raise _refuse(f"{resolver} takes {', '.join(found.params)}")
    return db.query(found.sql, tuple(params))
