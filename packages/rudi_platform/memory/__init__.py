"""
Conversation memory (blueprint §7.2): state.py (one conversation's state and its flat stored
form), store.py (the ConversationStore seam, in-memory implementation), legacy_s3.py (CM's
legacy S3 layout).
"""

from .legacy_s3 import LegacyS3ConversationStore
from .state import ConversationState, from_flat, to_flat
from .store import ConversationStore, InMemoryConversationStore, StoredConversation

__all__ = [
    "ConversationState",
    "ConversationStore",
    "InMemoryConversationStore",
    "LegacyS3ConversationStore",
    "StoredConversation",
    "from_flat",
    "to_flat",
]
