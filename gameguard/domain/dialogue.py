"""Dialogue · NPC 对话树数据模型。

本模块的职责

对话在米哈游类游戏里是有向图，不是线性流：
  - 玩家在每个 `DialogueNode` 面前会看到若干 `Choice` 选项
  - 每个 Choice 引向下一个 node（或 `None` 表示对话结束）
  - 某些 Choice 有 `requires_flag` 前置（例如只有"结盟船长"后才能选"提到船长"）
  - 某些 Choice 会 `sets_flag`（让 quest 进下一步）

数据结构：
  - `DialogueGraph`：一个 NPC 的对话树 = nodes dict + root_node_id
  - `DialogueNode`：单次对话展示内容 + 玩家可选 Choice 列表
  - `Choice`：选项文本 + 下一节点 + flag 前置/后置

不放的东西：
  - 运行时状态（在 `dialogue_runtime.py`）
  - NPC 本身（用 Entity + dialogue_graph_id）

对话无环要求（I-09 oracle）

对话图允许"汇流"（多个 choice 路径到同一节点），不允许环。
`dialogue_no_dead_branch` invariant 做两件事：
  (a) 每个 DialogueNode 从 root 可达（无孤岛）
  (b) 每个非叶 node 能到某个"end"节点（无死胡同）

这两条都是图遍历，evaluator 在 D17 实现。
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

# --- Choice · 对话选项 ---
class Choice(BaseModel):
    """对话中的一个选项。

    - label：玩家看到的文本
    - next_node：选择后跳到哪个 DialogueNode 的 id；None 表示对话结束
    - requires_flag：展示这个选项的前提 flag（None = 始终可见）
    - sets_flag：选中这个选项时自动 set 的 flag（(key, value) 对或 None）
    """

    label: str
    next_node: str | None = None
    requires_flag: str | None = None
    sets_flag: tuple[str, object] | None = Field(
        default=None,
        description="(flag_key, value) tuple；例 ('quest.harbor.ally_chosen','captain')",
    )

    model_config = ConfigDict(arbitrary_types_allowed=True)

# --- DialogueNode · 对话节点 ---
class DialogueNode(BaseModel):
    """一个对话节点 = 说话者 + 文本 + 玩家可选的选项列表。"""

    id: str
    speaker: str = Field("", description="说话者 id（通常是 NPC entity id）")
    text: str = Field("", description="展示给玩家的文本")
    choices: list[Choice] = Field(
        default_factory=list,
        description="玩家可选项；空列表表示对话自动结束",
    )

    def is_terminal(self) -> bool:
        """是否叶节点（无 choice 或所有 choice 都无 next_node）。"""
        return not self.choices or all(c.next_node is None for c in self.choices)

# --- DialogueGraph · 一个 NPC 的完整对话树 ---
class DialogueGraph(BaseModel):
    """一棵对话树（可能有汇流，但不能有环）。

    属于某个 NPC（npc_id）；一个 NPC 可以有多棵对话树，对应不同任务阶段。
    """

    id: str = Field(..., description="对话图 id，例 'captain_intro'")
    npc_id: str = Field(..., description="对话 NPC 的 entity id")
    root_node_id: str = Field(..., description="开始节点")
    nodes: dict[str, DialogueNode] = Field(default_factory=dict)

    # -------- 查询 --------

    def get_node(self, node_id: str) -> DialogueNode:
        if node_id not in self.nodes:
            raise KeyError(f"DialogueGraph {self.id!r} 无节点 {node_id!r}")
        return self.nodes[node_id]

    def all_node_ids(self) -> set[str]:
        return set(self.nodes.keys())

    # -------- 可达性分析（I-09 用） --------

    def reachable_from_root(self) -> set[str]:
        """从 root 做 BFS，返回所有可达节点 id。"""
        visited: set[str] = set()
        queue: list[str] = [self.root_node_id]
        while queue:
            cur = queue.pop(0)
            if cur in visited or cur not in self.nodes:
                continue
            visited.add(cur)
            for c in self.nodes[cur].choices:
                if c.next_node is not None and c.next_node in self.nodes:
                    queue.append(c.next_node)
        return visited

    def nodes_that_can_reach_terminal(self) -> set[str]:
        """反向图 BFS：返回能到达某个 terminal node 的所有节点 id。

        用于检测"死胡同" —— 如果某非 terminal 节点不在此集合里，说明对话
        进入后无路可走。
        """
        # 先建反向邻接表
        reverse: dict[str, list[str]] = {nid: [] for nid in self.nodes}
        for nid, node in self.nodes.items():
            for c in node.choices:
                if c.next_node is not None and c.next_node in self.nodes:
                    reverse[c.next_node].append(nid)

        # 从所有 terminal 反向 BFS
        terminals = {nid for nid, n in self.nodes.items() if n.is_terminal()}
        can_reach: set[str] = set(terminals)
        queue = list(terminals)
        while queue:
            cur = queue.pop(0)
            for prev in reverse.get(cur, []):
                if prev not in can_reach:
                    can_reach.add(prev)
                    queue.append(prev)
        return can_reach
