"""
MEET_RUDI Training Arena — named accounts, passwords and sessions.

Same posture as the tester console: PBKDF2-HMAC-SHA256 with a per-user salt, opaque session tokens
stored only as SHA-256 digests, idle expiry checked on every request, lockout after repeated
failures, and no user enumeration. There is no e-mail anywhere: a super-admin creates an account
or resets a password, the arena shows a one-time password once, and the user must choose their
own at the next sign-in.

The first super-admin comes from the secret meetrudi/arena/bootstrap ({"username", "password"}).
Until it exists — and no account exists — sign-in fails closed.
"""

import os
import re
import json
import hmac
import time
import base64
import hashlib
import secrets as _secrets

from arena_common import iso, new_code

ROUNDS = int(os.environ.get("ARENA_PBKDF2_ROUNDS", "600000"))
IDLE_MIN = int(os.environ.get("ARENA_SESSION_IDLE_MIN", "30"))
MAX_FAILED = int(os.environ.get("ARENA_MAX_FAILED_LOGINS", "10"))
USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,31}$")
ROLES = ("tester", "superadmin")


def hash_password(pw, rounds=None, salt=None):
    salt = salt or _secrets.token_bytes(16)
    rounds = int(rounds or ROUNDS)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, rounds)
    return {"salt": base64.b64encode(salt).decode(), "hash": base64.b64encode(dk).decode(), "rounds": rounds}


def check_password(pw, rec):
    if not rec or not rec.get("hash"):
        hashlib.pbkdf2_hmac("sha256", b"x", b"saltsaltsaltsalt", ROUNDS)   # same work either way
        return False
    dk = hashlib.pbkdf2_hmac("sha256", (pw or "").encode("utf-8"), base64.b64decode(rec["salt"]), int(rec["rounds"]))
    return hmac.compare_digest(base64.b64encode(dk).decode(), rec["hash"])


def policy_ok(pw):
    return (isinstance(pw, str) and len(pw) >= 12 and re.search(r"[a-z]", pw) and re.search(r"[A-Z]", pw)
            and re.search(r"\d", pw)) is not None and len(pw) >= 12


def temp_password():
    """One-time password: four groups of four. Exempt from the policy because it must be changed."""
    return "-".join(new_code(4) for _ in range(4))


def digest(token):
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


class AuthError(Exception):
    def __init__(self, status, code, detail=""):
        super().__init__(code)
        self.status, self.code, self.detail = status, code, detail


class Auth:
    def __init__(self, store, secrets_client=None, bootstrap_secret="meetrudi/arena/bootstrap", clock=time.time):
        self.store, self.secrets, self.bootstrap_secret, self.clock = store, secrets_client, bootstrap_secret, clock

    # ------------------------------------------------------------------ bootstrap
    def ensure_bootstrap(self):
        """True when at least one account exists (creating the first super-admin if needed)."""
        if self.store.users():
            return True
        if not self.secrets:
            return False
        try:
            raw = self.secrets.get_secret_value(SecretId=self.bootstrap_secret).get("SecretString") or "{}"
            cfg = json.loads(raw)
        except Exception:  # noqa: BLE001 - no secret yet: fail closed
            return False
        name, pw = str(cfg.get("username") or "").lower(), cfg.get("password") or ""
        if not USERNAME_RE.match(name) or not pw:
            return False
        self.store.put_user({"u": name, "role": "superadmin", "status": "must_change", "pw": hash_password(pw),
                             "failed": 0, "created": iso(), "by": "bootstrap", "last": "", "note": "bootstrap account"})
        self.store.audit("system", "user.bootstrap", name)
        return True

    # ------------------------------------------------------------------ sign-in
    def login(self, username, password):
        if not self.ensure_bootstrap():
            raise AuthError(503, "auth_not_configured", "No account exists yet and the bootstrap secret is missing.")
        name = str(username or "").strip().lower()
        u = self.store.user(name) if USERNAME_RE.match(name) else None
        ok = check_password(password or "", (u or {}).get("pw"))
        if u and u.get("status") == "locked":
            raise AuthError(403, "account_locked", "This account is locked. Ask a super-admin to unlock it.")
        if not u or not ok:
            if u:
                u["failed"] = int(u.get("failed") or 0) + 1
                if u["failed"] >= MAX_FAILED:
                    u["status"], u["note"] = "locked", "%d failed sign-ins" % u["failed"]
                    self.store.audit("system", "user.locked", name, u["note"])
                self.store.put_user(u)
            raise AuthError(401, "invalid_credentials", "Wrong username or password.")
        token = _secrets.token_urlsafe(32)
        now = self.clock()
        self.store.put_session(digest(token), {"u": name, "role": u["role"], "created": now, "seen": now})
        u.update({"failed": 0, "last": iso()})
        self.store.put_user(u)
        self.store.audit(name, "login.success")
        return token, self._public(u)

    def session(self, token):
        """(session, user) for a live token, else None. Touches the idle clock."""
        if not token:
            return None
        d = digest(token)
        s = self.store.session(d)
        now = self.clock()
        if not s or now - float(s.get("seen") or 0) > IDLE_MIN * 60:
            if s:
                self.store.delete_session(d)
            return None
        u = self.store.user(s["u"])
        if not u or u.get("status") == "locked":
            self.store.delete_session(d)
            return None
        if now - float(s["seen"]) > 30:
            s["seen"] = now
            self.store.put_session(d, s)
        return s, u

    def logout(self, token):
        self.store.delete_session(digest(token))

    def change_password(self, user, current, new):
        if not check_password(current or "", user.get("pw")):
            raise AuthError(401, "invalid_credentials", "The current password is not right.")
        if not policy_ok(new):
            raise AuthError(400, "weak_password", "At least 12 characters, with a lower-case letter, an upper-case letter and a digit.")
        user.update({"pw": hash_password(new), "status": "active", "note": "", "failed": 0})
        self.store.put_user(user)
        self.store.audit(user["u"], "account.password_changed", user["u"])

    # ------------------------------------------------------------------ administration
    def _kill_sessions(self, name):
        for d, s in self.store.sessions():
            if s and s.get("u") == name:
                self.store.delete_session(d)

    def _last_admin(self, name):
        admins = [u for u in self.store.users() if u.get("role") == "superadmin" and u.get("status") != "locked"]
        return len(admins) <= 1 and any(u["u"] == name for u in admins)

    def create(self, actor, name, role):
        name = str(name or "").strip().lower()
        if not USERNAME_RE.match(name):
            raise AuthError(400, "bad_username", "Use 3–32 characters: a–z, 0–9, dot, dash or underscore.")
        if role not in ROLES:
            raise AuthError(400, "bad_role", "Role is tester or superadmin.")
        if self.store.user(name):
            raise AuthError(409, "taken", "That username is taken.")
        pw = temp_password()
        self.store.put_user({"u": name, "role": role, "status": "must_change", "pw": hash_password(pw), "failed": 0,
                             "created": iso(), "by": actor, "last": "", "note": "temporary password not yet changed"})
        self.store.audit(actor, "user.create", name, "role %s · temporary password issued" % role)
        return pw

    def reset(self, actor, name):
        u = self._get(name)
        pw = temp_password()
        u.update({"pw": hash_password(pw), "status": "must_change", "failed": 0, "note": "temporary password not yet changed"})
        self.store.put_user(u)
        self._kill_sessions(name)
        self.store.audit(actor, "user.reset_password", name)
        return pw

    def lock(self, actor, name, locked):
        u = self._get(name)
        if locked and (name == actor or self._last_admin(name)):
            raise AuthError(409, "refused", "You cannot lock yourself or the last super-admin.")
        u["status"] = "locked" if locked else ("must_change" if u.get("status") == "must_change" else "active")
        u["note"] = ("locked by %s" % actor) if locked else ""
        u["failed"] = 0
        self.store.put_user(u)
        if locked:
            self._kill_sessions(name)
        self.store.audit(actor, "user.lock" if locked else "user.unlock", name)

    def set_role(self, actor, name, role):
        u = self._get(name)
        if role not in ROLES:
            raise AuthError(400, "bad_role", "Role is tester or superadmin.")
        if role != "superadmin" and (name == actor or self._last_admin(name)):
            raise AuthError(409, "refused", "You cannot demote yourself or the last super-admin.")
        u["role"] = role
        self.store.put_user(u)
        self._kill_sessions(name)
        self.store.audit(actor, "user.role", name, "now " + role)

    def delete(self, actor, name):
        self._get(name)
        if name == actor or self._last_admin(name):
            raise AuthError(409, "refused", "You cannot delete yourself or the last super-admin.")
        self.store.delete_user(name)
        self._kill_sessions(name)
        self.store.audit(actor, "user.delete", name)

    def _get(self, name):
        u = self.store.user(str(name or "").lower())
        if not u:
            raise AuthError(404, "unknown_user", "No such user.")
        return u

    @staticmethod
    def _public(u):
        return {"u": u["u"], "role": u["role"], "status": u.get("status"), "must_change": u.get("status") == "must_change"}
