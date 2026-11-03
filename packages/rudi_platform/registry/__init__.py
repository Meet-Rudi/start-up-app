"""
The registry (blueprint §7.1): what tenants, clones, versions, personas, endpoints, skills,
accounts and roles exist. models.py (records and contexts), store.py (the RegistryStore seam and
its errors), memory.py (in-memory implementation), publishing.py (draft → staging → live).
"""

from .memory import InMemoryRegistryStore
from .models import PLATFORM, PlatformContext, TenantContext
from .publishing import (
    PublishRefused,
    PublishResult,
    approve_live,
    check_version,
    publish_to_staging,
)
from .store import (
    Conflict,
    Forbidden,
    InvalidState,
    InvalidValue,
    NotFound,
    RegistryError,
    RegistryStore,
)

__all__ = [
    "PLATFORM",
    "Conflict",
    "Forbidden",
    "InMemoryRegistryStore",
    "InvalidState",
    "InvalidValue",
    "NotFound",
    "PlatformContext",
    "PublishRefused",
    "PublishResult",
    "RegistryError",
    "RegistryStore",
    "TenantContext",
    "approve_live",
    "check_version",
    "publish_to_staging",
]
