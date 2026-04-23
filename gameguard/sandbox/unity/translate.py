"""protobuf ↔ domain 双向翻译 helper（Stage 6 · D19）。

mock_server 和 UnityAdapter 两端共享这些函数，避免协议逻辑 drift。

设计要点
========

**PySim 字段强类型**：Character / BuffInstance 字段都有明确 proto 消息，
一一对应 pydantic model，翻译是纯字段拷贝。

**QuestSim 字段走 JSON 透传**：scene / quest / entities / nav_grid / save
这些 runtime 对象的字段形状变动频繁（D13-D17 每迭代一次都加字段），塞进
``StateResponse.custom_fields`` 的 bytes 里（UTF-8 JSON），proto schema
就不用每次改动。缺点是失去 protobuf 的强类型检查——但对我们"mock 端都是
Python"的场景完全可接受。

**QuestSim Action 走 GenericAction**：move_to / interact / dialogue / save
/ load 五种 action 都序列化成 ``{kind, payload_json}`` 放进
``Action.generic``。新增 action 时不用改 proto。

为什么这么分？—— PySim 的 Character 是整个 QA 故事的主角（hp/mp/buffs/
cooldowns 都是 invariant 的核心断言对象），proto 强类型让跨语言消费
（Python ↔ C#）最安全；QuestSim 的场景对象是"大而松"的，强制上 proto
schema 成本高于收益。
"""
from __future__ import annotations

import json
from typing import Any

from gameguard.domain import (
    Action,
    ActionOutcome,
    Character,
    CharacterState,
    Event,
    EventLog,
)
from gameguard.domain.action import (
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
from gameguard.domain.buff import BuffInstance
from gameguard.sandbox.adapter import AdapterInfo, SandboxState, StepResult
from gameguard.sandbox.unity.generated import gameguard_v1_pb2 as pb


# =============================================================================
# Action：domain → proto / proto → domain
# =============================================================================

def action_to_proto(action: Action) -> pb.Action:
    """把 domain.Action 翻成 proto.Action（oneof）。"""
    msg = pb.Action()
    if isinstance(action, CastAction):
        msg.cast.CopyFrom(pb.CastAction(
            actor=action.actor, skill=action.skill, target=action.target
        ))
    elif isinstance(action, WaitAction):
        msg.wait.CopyFrom(pb.WaitAction(seconds=action.seconds))
    elif isinstance(action, InterruptAction):
        msg.interrupt.CopyFrom(pb.InterruptAction(actor=action.actor))
    elif isinstance(action, NoopAction):
        msg.noop.CopyFrom(pb.NoopAction())
    elif isinstance(action, MoveToAction | InteractAction | DialogueAction
                            | SaveAction | LoadAction):
        # QuestSim 家族走 generic JSON 透传，proto schema 不用跟 QuestSim
        # action 字段同步演进
        msg.generic.CopyFrom(pb.GenericAction(
            kind=action.kind,
            payload_json=action.model_dump_json(),
        ))
    else:
        raise TypeError(f"未知 Action 类型：{type(action).__name__}")
    return msg


# QuestSim generic action 的 kind → 具体 Pydantic 类的映射表
_GENERIC_ACTION_CLASSES: dict[str, type[Any]] = {
    "move_to": MoveToAction,
    "interact": InteractAction,
    "dialogue": DialogueAction,
    "save": SaveAction,
    "load": LoadAction,
}


def proto_to_action(msg: pb.Action) -> Action:
    """把 proto.Action 翻回 domain.Action。"""
    which = msg.WhichOneof("variant")
    if which == "cast":
        return CastAction(
            actor=msg.cast.actor, skill=msg.cast.skill, target=msg.cast.target
        )
    if which == "wait":
        return WaitAction(seconds=msg.wait.seconds)
    if which == "interrupt":
        return InterruptAction(actor=msg.interrupt.actor)
    if which == "noop":
        return NoopAction()
    if which == "generic":
        cls = _GENERIC_ACTION_CLASSES.get(msg.generic.kind)
        if cls is None:
            raise ValueError(f"未知 generic action kind: {msg.generic.kind!r}")
        return cls.model_validate_json(msg.generic.payload_json)
    raise ValueError(f"Action oneof 未设置：{msg}")


# =============================================================================
# SandboxState：domain → proto / proto → domain
# =============================================================================

def state_to_proto(state: SandboxState, custom_state: Any = None) -> pb.StateResponse:
    """把 SandboxState 翻成 proto.StateResponse。

    ``custom_state``：可选的 QuestSim 扩展状态（scene/quest/entities 的
    JSON 可序列化 dict），走 custom_fields bytes 透传。pysim 传 None。
    """
    resp = pb.StateResponse(
        t=state.t,
        tick=state.tick,
        seed=state.seed,
        rng_draws=state.rng_draws,
    )
    for cid, ch in state.characters.items():
        resp.characters.append(_character_to_proto(ch))
    if custom_state is not None:
        resp.custom_fields = json.dumps(custom_state, ensure_ascii=False).encode("utf-8")
    return resp


def proto_to_state(msg: pb.StateResponse) -> tuple[SandboxState, Any]:
    """把 proto.StateResponse 翻回 (SandboxState, custom_state_dict)。"""
    chars: dict[str, Character] = {}
    for cm in msg.characters:
        chars[cm.id] = _proto_to_character(cm)
    state = SandboxState(
        t=msg.t,
        tick=msg.tick,
        seed=msg.seed,
        characters=chars,
        rng_draws=msg.rng_draws,
    )
    custom_state: Any = None
    if msg.custom_fields:
        custom_state = json.loads(msg.custom_fields.decode("utf-8"))
    return state, custom_state


def _character_to_proto(ch: Character) -> pb.Character:
    cm = pb.Character(
        id=ch.id,
        name=ch.name,
        hp=ch.hp,
        hp_max=ch.hp_max,
        mp=ch.mp,
        mp_max=ch.mp_max,
        state=ch.state.value,
        casting_skill=ch.casting_skill or "",
        cast_remaining=ch.cast_remaining,
    )
    for skill_id, cd in ch.cooldowns.items():
        cm.cooldowns[skill_id] = cd
    for buff in ch.buffs:
        cm.buffs.append(pb.BuffInstance(
            spec_id=buff.spec_id,
            magnitude=buff.magnitude,
            remaining=buff.remaining,
            stacks=buff.stacks,
            source_id=buff.source_id or "",
            applied_at=buff.applied_at,
        ))
    return cm


def _proto_to_character(cm: pb.Character) -> Character:
    return Character(
        id=cm.id,
        name=cm.name,
        hp=cm.hp,
        hp_max=cm.hp_max,
        mp=cm.mp,
        mp_max=cm.mp_max,
        state=CharacterState(cm.state) if cm.state else CharacterState.IDLE,
        cooldowns=dict(cm.cooldowns),
        buffs=[
            BuffInstance(
                spec_id=b.spec_id,
                magnitude=b.magnitude,
                remaining=b.remaining,
                stacks=b.stacks,
                source_id=b.source_id or None,
                applied_at=b.applied_at,
            )
            for b in cm.buffs
        ],
        casting_skill=cm.casting_skill or None,
        cast_remaining=cm.cast_remaining,
    )


# =============================================================================
# Event / EventLog：单向（server → client 的 streaming）
# =============================================================================

def event_to_proto(e: Event) -> pb.Event:
    return pb.Event(
        tick=e.tick,
        t=e.t,
        kind=e.kind,
        actor=e.actor or "",
        target=e.target or "",
        skill=e.skill or "",
        buff=e.buff or "",
        amount=e.amount or 0.0,
        meta_json=json.dumps(e.meta, ensure_ascii=False) if e.meta else "",
    )


def proto_to_event(msg: pb.Event) -> Event:
    return Event(
        tick=msg.tick,
        t=msg.t,
        kind=msg.kind,  # type: ignore[arg-type]
        actor=msg.actor or None,
        target=msg.target or None,
        skill=msg.skill or None,
        buff=msg.buff or None,
        amount=msg.amount if msg.amount else None,
        meta=json.loads(msg.meta_json) if msg.meta_json else {},
    )


def eventlog_to_proto_batch(log: EventLog, start: int = 0) -> pb.EventBatch:
    """把 EventLog 从 start 索引开始打包成一个 EventBatch。"""
    return pb.EventBatch(events=[event_to_proto(e) for e in log.events[start:]])


# =============================================================================
# StepResult / AdapterInfo
# =============================================================================

def step_result_to_proto(
    result: StepResult, custom_state: Any = None
) -> pb.StepResponse:
    return pb.StepResponse(
        state=state_to_proto(result.state, custom_state),
        accepted=result.outcome.accepted,
        reason=result.outcome.reason or "",
        new_events=result.new_events,
        done=result.done,
    )


def proto_to_step_result(msg: pb.StepResponse) -> tuple[StepResult, Any]:
    state, custom = proto_to_state(msg.state)
    return StepResult(
        state=state,
        outcome=ActionOutcome(accepted=msg.accepted, reason=msg.reason or None),
        new_events=msg.new_events,
        done=msg.done,
    ), custom


def adapter_info_to_proto(info: AdapterInfo, engine_version: str = "",
                           project_name: str = "") -> pb.AdapterInfo:
    return pb.AdapterInfo(
        name=info.name,
        version=info.version,
        deterministic=info.deterministic,
        engine_version=engine_version,
        project_name=project_name,
    )


def proto_to_adapter_info(msg: pb.AdapterInfo) -> AdapterInfo:
    return AdapterInfo(
        name=msg.name,
        version=msg.version,
        deterministic=msg.deterministic,
    )
