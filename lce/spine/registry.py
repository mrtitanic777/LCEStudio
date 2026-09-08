"""The LCEStudio capability registry — the plug-and-play spine.

Every user-facing capability (convert a world, paint a map, find structures, generate
terrain, render, …) is described once as a `Capability` and registered here. The GUI,
CLI, API and any future front-end then DISCOVER capabilities instead of hard-wiring each
one, so adding a feature is: write its function, register it, and it shows up everywhere.

    from lce import registry as R

    @R.capability("convert.title_update", "Change title update", "convert",
                  "Retarget a console/Windows-LCE save to any TU0-75.")
    def _tu(session=None, path=None, target_tu=19, **kw):
        ...

    R.all()                 # every capability
    R.by_category("convert")
    R.get("convert.title_update").run(path="…", target_tu=25)

A capability is pure metadata + a callable; it pulls in no heavy dependency until run.
"""
from dataclasses import dataclass, field
from typing import Callable, Dict, List

# capability categories, in a sensible display order
CATEGORIES = ["edit", "generate", "render", "convert", "analyze", "tools"]


@dataclass
class Param:
    """One input a capability accepts."""
    name: str
    type: str = "str"                 # "str" | "int" | "bool" | "float" | "path" | "choice"
    default: object = None
    help: str = ""
    choices: tuple = ()
    required: bool = False


@dataclass
class Capability:
    name: str                         # unique dotted id, e.g. "convert.title_update"
    title: str                        # human-facing title
    category: str                     # one of CATEGORIES
    summary: str = ""
    run: Callable = None              # run(session=?, path=?, **params) -> result
    params: List[Param] = field(default_factory=list)
    scope: str = "session"            # "session" (needs an open world) | "path" (works on a file)
    gui: bool = True                  # offer in the GUI
    cli: str = ""                     # CLI subcommand name (blank = not on the CLI)
    result: str = "text"             # hint at the result kind: "text"|"path"|"image"|"world"|"data"

    def __call__(self, *a, **kw):
        return self.run(*a, **kw)


_REGISTRY: Dict[str, Capability] = {}


def register(cap: Capability, replace: bool = False) -> Capability:
    """Add a Capability. Raises on a duplicate name unless `replace`."""
    if cap.category not in CATEGORIES:
        raise ValueError("unknown category %r (use one of %s)" % (cap.category, CATEGORIES))
    if cap.name in _REGISTRY and not replace:
        raise ValueError("capability %r already registered" % cap.name)
    _REGISTRY[cap.name] = cap
    return cap


def capability(name, title, category, summary="", *, params=None, scope="session",
               gui=True, cli="", result="text", replace=False):
    """Decorator: register the wrapped function as a Capability and return it unchanged."""
    def deco(fn):
        register(Capability(name=name, title=title, category=category, summary=summary,
                            run=fn, params=list(params or []), scope=scope, gui=gui,
                            cli=cli, result=result), replace=replace)
        return fn
    return deco


def get(name: str) -> Capability:
    return _REGISTRY[name]


def has(name: str) -> bool:
    return name in _REGISTRY


def all() -> List[Capability]:
    """Every capability, grouped by category order then title."""
    return sorted(_REGISTRY.values(),
                  key=lambda c: (CATEGORIES.index(c.category) if c.category in CATEGORIES else 99,
                                 c.title.lower()))


def by_category(category: str) -> List[Capability]:
    return [c for c in all() if c.category == category]


def categories() -> List[str]:
    """Categories that actually have at least one capability, in display order."""
    present = {c.category for c in _REGISTRY.values()}
    return [c for c in CATEGORIES if c in present]


def cli_commands() -> Dict[str, Capability]:
    """Capabilities that expose a CLI subcommand -> {cli_name: capability}."""
    return {c.cli: c for c in _REGISTRY.values() if c.cli}


def clear():
    """Drop every registration (used by tests)."""
    _REGISTRY.clear()
