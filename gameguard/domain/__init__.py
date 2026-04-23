"""Domain models shared across the sandbox, agents, and reports layers.

Everything here is pure data (Pydantic). No I/O, no LLM calls, no game
logic — game logic lives in ``gameguard.sandbox.pysim``.
"""
from gameguard.domain.action import (
    Action,
    ActionOutcome,
    CastAction,
    DialogueAction,
    InteractAction,
    InterruptAction,
    LoadAction,
    MoveToAction,
    NoopAction,
    SaveAction,
    WaitAction,
)
from gameguard.domain.buff import BuffBook, BuffInstance, BuffSpec, StackRule
from gameguard.domain.character import Character, CharacterState
from gameguard.domain.entity import Entity, EntityKind, EntityRegistry
from gameguard.domain.event import Event, EventKind, EventLog
from gameguard.domain.geom import (
    BoundingBox,
    GridCoord,
    Vec3,
    aabb_contains_point,
    aabb_intersects,
    segment_intersects_aabb,
)
from gameguard.domain.skill import DamageType, SkillBook, SkillSpec

__all__ = [
    "Action",
    "ActionOutcome",
    "BoundingBox",
    "BuffBook",
    "BuffInstance",
    "BuffSpec",
    "CastAction",
    "Character",
    "CharacterState",
    "DamageType",
    "DialogueAction",
    "Entity",
    "EntityKind",
    "EntityRegistry",
    "Event",
    "EventKind",
    "EventLog",
    "GridCoord",
    "InteractAction",
    "InterruptAction",
    "LoadAction",
    "MoveToAction",
    "NoopAction",
    "SaveAction",
    "SkillBook",
    "SkillSpec",
    "StackRule",
    "Vec3",
    "WaitAction",
    "aabb_contains_point",
    "aabb_intersects",
    "segment_intersects_aabb",
]
