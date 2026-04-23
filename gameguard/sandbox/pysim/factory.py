"""Factory helpers that wire up a ready-to-run PySim.

Keeps agents and tests decoupled from which handler version they get;
they just pick the version string and receive a GameAdapter.
"""
from __future__ import annotations

from gameguard.domain import Character, CharacterState
from gameguard.sandbox.adapter import GameAdapter
from gameguard.sandbox.pysim.core import PySim
from gameguard.sandbox.pysim.v1 import V1SkillHandler, build_buff_book, build_skill_book
from gameguard.sandbox.pysim.v2 import V2SkillHandler


def default_characters() -> list[Character]:
    """Two-character roster: the player caster and a training dummy target."""
    return [
        Character(
            id="p1",
            name="Player",
            hp=500.0,
            hp_max=500.0,
            mp=100.0,
            mp_max=100.0,
            state=CharacterState.IDLE,
        ),
        Character(
            id="dummy",
            name="Training Dummy",
            hp=1000.0,
            hp_max=1000.0,
            mp=0.0,
            mp_max=0.0,
            state=CharacterState.IDLE,
        ),
    ]


def make_sandbox(version: str = "v1") -> GameAdapter:
    """按版本字符串构造一个 GameAdapter。

    支持的版本：
      - ``'v1'``: 黄金参考实现
      - ``'v2'``: 故意植入 5 个 bug 的实现（见 sandbox/pysim/v2/skills.py）

    v1 / v2 共享数据表和角色初始状态——只在 SkillHandler 行为上不同，
    模拟真实迭代里"策划没改表，程序员改了实现"的 regression 引入路径。
    """
    skills = build_skill_book()
    buffs = build_buff_book()
    characters = default_characters()

    handlers = {
        "v1": (V1SkillHandler, "pysim-v1"),
        "v2": (V2SkillHandler, "pysim-v2"),
    }
    if version not in handlers:
        raise ValueError(
            f"unknown version {version!r}; supported: {list(handlers)}"
        )
    cls, label = handlers[version]
    return PySim(
        version=label,
        skill_book=skills,
        buff_book=buffs,
        initial_characters=characters,
        handler=cls(),
    )
