"""
Access as data (decision #33): the permission catalogue and seed roles (catalogue.py), the
decisions over them (policy.py), and the seed migration generator (seed.py).
"""

from .catalogue import PERMISSIONS, SEED_ROLES, Permission, Role, Scope
from .policy import AccessError, AccessPolicy, Assignment, Decision, validate_role

__all__ = [
    "PERMISSIONS",
    "SEED_ROLES",
    "AccessError",
    "AccessPolicy",
    "Assignment",
    "Decision",
    "Permission",
    "Role",
    "Scope",
    "validate_role",
]
