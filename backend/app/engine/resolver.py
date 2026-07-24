"""Node input resolution.

Node inputs are declared as data, not code, so the whole wiring of a workflow
stays inspectable:

    "inputs": {
        "message":  "$.run.input.message",
        "customer": "$.nodes.fetch_context.output",
        "reply":    {"first_of": ["$.nodes.draft_bug.output.body",
                                  "$.nodes.draft_billing.output.body"]},
        "channel":  {"const": "email"},
    }

Paths that point at a skipped/unstarted node resolve to ``None`` rather than
raising - that is what lets a join node read "whichever branch actually ran".
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from app.engine.errors import ResolutionError


@dataclass
class NodeView:
    status: str
    output: dict[str, Any] | None = None


@dataclass
class ResolutionContext:
    run_id: str
    workflow_id: str
    run_input: dict[str, Any]
    nodes: dict[str, NodeView] = field(default_factory=dict)
    _root: dict[str, Any] | None = field(default=None, repr=False, compare=False)

    def as_mapping(self) -> dict[str, Any]:
        """The document paths are resolved against.

        Built once per context - a node with several inputs would otherwise
        rebuild the whole node map for every path it looks up.
        """
        if self._root is None:
            self._root = {
                "run": {
                    "id": self.run_id,
                    "workflow_id": self.workflow_id,
                    "input": self.run_input,
                },
                "nodes": {
                    node_id: {"status": view.status, "output": view.output}
                    for node_id, view in self.nodes.items()
                },
            }
        return self._root


def _walk(root: Any, path: str) -> Any:
    current: Any = root
    for part in path.split("."):
        if current is None:
            return None
        if isinstance(current, Mapping):
            if part not in current:
                return None
            current = current[part]
        elif isinstance(current, (list, tuple)):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            current = getattr(current, part, None)
    return current


def resolve_path(path: str, ctx: ResolutionContext) -> Any:
    if not path.startswith("$."):
        raise ResolutionError(f"path expression must start with '$.': {path!r}")
    return _walk(ctx.as_mapping(), path[2:])


def resolve_value(spec: Any, ctx: ResolutionContext) -> Any:
    """Resolve one input spec.

    Supported forms:
      "$.a.b.c"                     -> path lookup (None if missing)
      {"const": <json>}             -> literal
      {"path": "$.a.b"}             -> explicit path
      {"first_of": [<spec>, ...]}   -> first non-None resolution
      {"all_of": [<spec>, ...]}     -> list of resolutions
      {"object": {k: <spec>}}       -> nested object
      anything else                 -> literal
    """
    if isinstance(spec, str):
        return resolve_path(spec, ctx) if spec.startswith("$.") else spec

    if isinstance(spec, Mapping):
        if "const" in spec:
            return spec["const"]
        if "path" in spec:
            value = resolve_path(spec["path"], ctx)
            if value is None and "default" in spec:
                return spec["default"]
            return value
        if "first_of" in spec:
            for candidate in spec["first_of"]:
                value = resolve_value(candidate, ctx)
                if value is not None:
                    return value
            return spec.get("default")
        if "all_of" in spec:
            return [resolve_value(item, ctx) for item in spec["all_of"]]
        if "object" in spec:
            return {k: resolve_value(v, ctx) for k, v in spec["object"].items()}
        # plain literal dict
        return dict(spec)

    if isinstance(spec, list):
        return [resolve_value(item, ctx) for item in spec]

    return spec


def resolve_inputs(
    specs: Mapping[str, Any] | None, ctx: ResolutionContext
) -> dict[str, Any]:
    if not specs:
        return {}
    return {name: resolve_value(spec, ctx) for name, spec in specs.items()}
