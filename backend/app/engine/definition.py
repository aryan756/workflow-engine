"""Declarative workflow DAG.

Workflows are *fixed and inspectable*: the node list, the edges, the branch
predicates and the agent output contracts are all static data. The only thing
decided at runtime is which branch label an agent/branch node selects.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from app.agents.contracts import CONTRACTS
from app.engine import predicates
from app.engine.errors import WorkflowDefinitionError


class NodeType(StrEnum):
    INPUT = "input"
    AGENT = "agent"
    BRANCH = "branch"
    TOOL = "tool"
    APPROVAL = "approval"


class JoinPolicy(StrEnum):
    #: every incoming edge must be active (a node after a fan-out)
    ALL = "all"
    #: at least one incoming edge must be active (a node joining branches)
    ANY = "any"


@dataclass(frozen=True)
class NodeDef:
    id: str
    type: NodeType
    title: str
    description: str = ""
    config: Mapping[str, Any] = field(default_factory=dict)
    join: JoinPolicy = JoinPolicy.ALL
    #: automatic attempts inside a single engine pass (transient failures)
    max_attempts: int = 1
    retry_backoff_seconds: float = 0.0


@dataclass(frozen=True)
class EdgeDef:
    source: str
    target: str
    #: when set, the edge is only active if the source (a branch node)
    #: selected this label
    label: str | None = None


@dataclass(frozen=True)
class WorkflowDefinition:
    id: str
    name: str
    description: str
    nodes: tuple[NodeDef, ...]
    edges: tuple[EdgeDef, ...]
    #: node whose output becomes the run output
    output_node: str
    version: str = "1"

    # -- lookups ---------------------------------------------------------
    def node(self, node_id: str) -> NodeDef:
        for n in self.nodes:
            if n.id == node_id:
                return n
        raise KeyError(node_id)

    def has_node(self, node_id: str) -> bool:
        return any(n.id == node_id for n in self.nodes)

    def incoming(self, node_id: str) -> tuple[EdgeDef, ...]:
        return tuple(e for e in self.edges if e.target == node_id)

    def outgoing(self, node_id: str) -> tuple[EdgeDef, ...]:
        return tuple(e for e in self.edges if e.source == node_id)

    def entry_nodes(self) -> tuple[NodeDef, ...]:
        return tuple(n for n in self.nodes if not self.incoming(n.id))

    # -- layout ----------------------------------------------------------
    def ranks(self) -> dict[str, int]:
        """Longest-path layering, used by the UI to draw the DAG left to right."""
        rank: dict[str, int] = {}
        for node_id in self.topological_order():
            preds = [e.source for e in self.incoming(node_id)]
            rank[node_id] = 0 if not preds else max(rank[p] for p in preds) + 1
        return rank

    def topological_order(self) -> list[str]:
        indegree = {n.id: 0 for n in self.nodes}
        for e in self.edges:
            indegree[e.target] += 1
        queue = [n.id for n in self.nodes if indegree[n.id] == 0]
        order: list[str] = []
        while queue:
            queue.sort()
            current = queue.pop(0)
            order.append(current)
            for e in self.outgoing(current):
                indegree[e.target] -= 1
                if indegree[e.target] == 0:
                    queue.append(e.target)
        if len(order) != len(self.nodes):
            remaining = sorted(set(indegree) - set(order))
            raise WorkflowDefinitionError(
                f"workflow '{self.id}' contains a cycle involving: {remaining}"
            )
        return order

    # -- validation ------------------------------------------------------
    def validate(self) -> None:
        ids = [n.id for n in self.nodes]
        duplicates = {i for i in ids if ids.count(i) > 1}
        if duplicates:
            raise WorkflowDefinitionError(f"duplicate node ids: {sorted(duplicates)}")

        for e in self.edges:
            if not self.has_node(e.source):
                raise WorkflowDefinitionError(f"edge source '{e.source}' is not a node")
            if not self.has_node(e.target):
                raise WorkflowDefinitionError(f"edge target '{e.target}' is not a node")

        if not self.entry_nodes():
            raise WorkflowDefinitionError("workflow has no entry node")

        if not self.has_node(self.output_node):
            raise WorkflowDefinitionError(f"output_node '{self.output_node}' is not a node")

        # Node configs first: the wiring checks below read branch cases, so a
        # malformed case would otherwise surface as a confusing wiring error.
        for n in self.nodes:
            self._validate_node_config(n)

        # labelled edges must originate from a branch node and reference a
        # label the branch can actually emit
        for e in self.edges:
            if e.label is None:
                continue
            source = self.node(e.source)
            if source.type is not NodeType.BRANCH:
                raise WorkflowDefinitionError(
                    f"edge {e.source}->{e.target} has label '{e.label}' but "
                    f"'{e.source}' is a {source.type.value} node, not a branch"
                )
            emitted = _branch_labels(source)
            if e.label not in emitted:
                raise WorkflowDefinitionError(
                    f"branch '{e.source}' never emits label '{e.label}' "
                    f"(emits: {sorted(emitted)})"
                )

        # every branch label must have somewhere to go
        for n in self.nodes:
            if n.type is not NodeType.BRANCH:
                continue
            wired = {e.label for e in self.outgoing(n.id) if e.label is not None}
            missing = _branch_labels(n) - wired
            if missing:
                raise WorkflowDefinitionError(
                    f"branch '{n.id}' emits unwired labels: {sorted(missing)}"
                )

        # a node with several incoming edges from *different* branch labels
        # needs an ANY join, otherwise it can never become ready
        for n in self.nodes:
            incoming = self.incoming(n.id)
            if len(incoming) > 1 and n.join is JoinPolicy.ALL:
                labelled = [e for e in incoming if e.label is not None]
                if labelled:
                    raise WorkflowDefinitionError(
                        f"node '{n.id}' joins conditional edges but uses join=all; "
                        "use join=any"
                    )

        self.topological_order()  # raises on cycles

    # -- per-node config checks -----------------------------------------
    @staticmethod
    def _validate_node_config(node: NodeDef) -> None:
        """Catch config typos at registration instead of mid-run.

        Without this, a misspelled contract name or branch operator only
        surfaces when a run reaches that node - by which point side effects
        upstream of it may already have happened.
        """
        config = node.config
        where = f"node '{node.id}'"

        def _require(key: str) -> Any:
            if not config.get(key):
                raise WorkflowDefinitionError(
                    f"{where} ({node.type.value}) requires config key '{key}'"
                )
            return config[key]

        def _check_contract(name: str) -> None:
            if name not in CONTRACTS:
                raise WorkflowDefinitionError(
                    f"{where} references unknown contract '{name}' "
                    f"(known: {sorted(CONTRACTS)})"
                )

        if node.type is NodeType.INPUT:
            if config.get("contract"):
                _check_contract(config["contract"])

        elif node.type is NodeType.AGENT:
            _require("task")
            _check_contract(_require("contract"))

        elif node.type is NodeType.TOOL:
            _require("tool")

        elif node.type is NodeType.BRANCH:
            cases = config.get("cases")
            if not isinstance(cases, (list, tuple)) or not cases:
                raise WorkflowDefinitionError(f"{where} needs a non-empty 'cases' list")
            for index, case in enumerate(cases):
                if not isinstance(case, Mapping) or not case.get("label"):
                    raise WorkflowDefinitionError(
                        f"{where} case {index} needs a 'label'"
                    )
                if "when" not in case:
                    raise WorkflowDefinitionError(
                        f"{where} case {index} ('{case['label']}') needs a 'when' predicate"
                    )
                try:
                    predicates.validate(case["when"], path=f"{where} case {index}")
                except predicates.PredicateError as exc:
                    raise WorkflowDefinitionError(str(exc)) from exc

        inputs = config.get("inputs")
        if inputs is not None and not isinstance(inputs, Mapping):
            raise WorkflowDefinitionError(f"{where} 'inputs' must be an object")

    def required_tools(self) -> set[str]:
        """Tool names this workflow will try to invoke."""
        return {
            str(n.config["tool"])
            for n in self.nodes
            if n.type is NodeType.TOOL and n.config.get("tool")
        }


def _branch_labels(node: NodeDef) -> set[str]:
    labels = {case["label"] for case in node.config.get("cases", [])}
    default = node.config.get("default")
    if default:
        labels.add(default)
    return labels


class WorkflowRegistry:
    """In-process registry of validated workflow definitions."""

    def __init__(self) -> None:
        self._items: dict[str, WorkflowDefinition] = {}

    def register(self, definition: WorkflowDefinition) -> WorkflowDefinition:
        definition.validate()
        self._items[definition.id] = definition
        return definition

    def get(self, workflow_id: str) -> WorkflowDefinition:
        if workflow_id not in self._items:
            raise KeyError(f"unknown workflow '{workflow_id}'")
        return self._items[workflow_id]

    def list(self) -> list[WorkflowDefinition]:
        return list(self._items.values())
