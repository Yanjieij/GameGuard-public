"""QuestSim 物理 backend 子包。"""
from gameguard.sandbox.questsim.physics.base import PhysicsBackend
from gameguard.sandbox.questsim.physics.dummy import DummyBackend


def make_physics_backend(name: str, *, tick_dt: float = 0.05) -> PhysicsBackend:
    """工厂：按名字装配 backend。pybullet 延迟 import 避免未装时 import 错。"""
    if name == "dummy":
        return DummyBackend()
    if name == "pybullet":
        from gameguard.sandbox.questsim.physics.pybullet_backend import PyBulletBackend
        return PyBulletBackend(tick_dt=tick_dt)
    raise ValueError(f"未知 physics backend: {name!r}")


__all__ = ["DummyBackend", "PhysicsBackend", "make_physics_backend"]
