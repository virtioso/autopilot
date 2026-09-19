"""Project drop-ins: a customer's chains, platforms and placements beside the
generic ones, without a line of theirs in this tree.

    projects/<name>/chains/*.json
    projects/<name>/platforms/*.yaml
    projects/<name>/placements/...

`projects/` is gitignored except its README: an entry there is a symlink a
workspace manifest placed (`repo`'s <linkfile> pointing at the customer's own
repository) or a checkout somebody made. autopilot resolves a bare name --
`--chain evk-bringup`, `--platform orin-agx` -- first in its own chains/ or
platforms/, then in every project's; a name two places define is refused,
never taken from the first directory listed, since the survivor would be
plausible and the loss invisible. A path is a path, as before.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROJECTS = ROOT / "projects"


class Ambiguous(Exception):
    """A bare name that resolves in more than one place."""


def projects() -> list[Path]:
    """Every project directory, sorted by name; a dangling symlink is skipped
    with no error here (the workspace tool that placed it reports it)."""
    if not PROJECTS.is_dir():
        return []
    return sorted(p for p in PROJECTS.iterdir() if p.is_dir() and not p.name.startswith("."))


def _resolve(name: str, kind: str, suffix: str) -> Path:
    """`name` as a path if it is one, else <kind>/<name><suffix> here or in
    exactly one project. Raises FileNotFoundError naming where it looked, or
    Ambiguous naming every match."""
    given = Path(name)
    if given.suffix == suffix and (given.exists() or "/" in name):
        return given
    hits = [p for p in [ROOT / kind / f"{name}{suffix}"] + [pr / kind / f"{name}{suffix}" for pr in projects()] if p.exists()]
    if len(hits) > 1:
        raise Ambiguous(f"{kind[:-1]} {name!r} is defined in {len(hits)} places: " + ", ".join(str(h.relative_to(ROOT)) for h in hits))
    if not hits:
        looked = [str((ROOT / kind).relative_to(ROOT))] + [str((pr / kind).relative_to(ROOT)) for pr in projects()]
        raise FileNotFoundError(f"no {kind[:-1]} {name!r} under {', '.join(looked)}")
    return hits[0]


def find_chain(name: str) -> Path:
    return _resolve(name, "chains", ".json")


def find_platform(name: str) -> Path:
    return _resolve(name, "platforms", ".yaml")


def list_chains() -> list[tuple[str, Path]]:
    """(label, path) for every chain here and in every project; a project's
    chain is labelled project/name so the listing says where it is."""
    out = [(p.stem, p) for p in sorted((ROOT / "chains").glob("*.json"))]
    for pr in projects():
        out += [(f"{pr.name}/{p.stem}", p) for p in sorted((pr / "chains").glob("*.json"))]
    return out
