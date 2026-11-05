"""
The Turn Engine (blueprint §3): prompt.py (slots and rendering), channels.py (what the engine
needs from a channel), skill.py (the real-time skill base and runtime), clone.py (a clone version
loaded for turns), turn.py (one turn), door.py (the Training Arena's door).
"""

from .channels import ChannelProfile, profile
from .clone import (
    AssetStore,
    CloneLoadError,
    DictAssetStore,
    LoadedClone,
    S3AssetStore,
    load_clone,
)
from .prompt import SLOTS, PromptPart, canonical, fill, render_system
from .skill import RealTimeSkill, SkillLoadError, SkillRuntime
from .turn import EngineError, TurnEngine, TurnOutcome

__all__ = [
    "SLOTS",
    "AssetStore",
    "ChannelProfile",
    "CloneLoadError",
    "DictAssetStore",
    "EngineError",
    "LoadedClone",
    "PromptPart",
    "RealTimeSkill",
    "S3AssetStore",
    "SkillLoadError",
    "SkillRuntime",
    "TurnEngine",
    "TurnOutcome",
    "canonical",
    "fill",
    "load_clone",
    "profile",
    "render_system",
]
