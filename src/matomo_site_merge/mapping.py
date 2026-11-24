"""Map the goals and custom dimensions of the source site onto the target.

Goal IDs and custom dimension IDs are numbered per site, and reports store
them by ID (Goal_<idgoal>_nb_conversions, customdimension<id>). Raw log rows
can be renumbered; archives cannot be rewritten (their blobs are compressed,
serialized PHP). So:

* raw logs: any mapping is fine, including onto a goal with another ID;
* archives that move: every goal or dimension of S they mention must map to
  the same ID in T. Where T has nothing at that ID, the goal or dimension is
  created in T with that ID.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Goal:
    idgoal: int
    name: str
    match_attribute: str = ""
    pattern: str = ""


@dataclass(frozen=True)
class Dimension:
    iddim: int
    name: str
    scope: str  # 'visit' or 'action'
    index: int  # column slot: custom_dimension_<index>


@dataclass
class Mapping:
    goals: dict[int, int] = field(default_factory=dict)  # S idgoal -> T idgoal
    create_goals: list[Goal] = field(default_factory=list)  # create in T, same idgoal
    dims: dict[int, int] = field(default_factory=dict)  # S iddim -> T iddim
    create_dims: list[Dimension] = field(default_factory=list)  # create in T
    errors: list[str] = field(default_factory=list)


def _norm(s: str) -> str:
    return " ".join(s.lower().split())


def map_goals(src: list[Goal], tgt: list[Goal], overrides: dict[int, int],
              archive_needs_identity: bool) -> tuple[dict[int, int], list[Goal], list[str]]:
    by_id = {g.idgoal: g for g in tgt}
    by_name: dict[str, list[Goal]] = {}
    for g in tgt:
        by_name.setdefault(_norm(g.name), []).append(g)
    result: dict[int, int] = {}
    create: list[Goal] = []
    errors: list[str] = []
    next_id = max([g.idgoal for g in tgt] + [g.idgoal for g in src] + [0]) + 1
    for g in sorted(src, key=lambda g: g.idgoal):
        if g.idgoal in overrides:
            t = overrides[g.idgoal]
            if t not in by_id:
                errors.append(f"--goal {g.idgoal}={t}: T has no goal {t}")
                continue
            result[g.idgoal] = t
        elif len(by_name.get(_norm(g.name), [])) == 1:
            result[g.idgoal] = by_name[_norm(g.name)][0].idgoal
        elif len(by_name.get(_norm(g.name), [])) > 1:
            errors.append(f"goal {g.idgoal} '{g.name}': T has several goals with this "
                          f"name; choose one with --goal {g.idgoal}=<id>")
            continue
        elif g.idgoal not in by_id:
            result[g.idgoal] = g.idgoal
            create.append(g)
        else:
            new = Goal(next_id, g.name, g.match_attribute, g.pattern)
            next_id += 1
            result[g.idgoal] = new.idgoal
            create.append(new)
        if archive_needs_identity and result.get(g.idgoal) != g.idgoal:
            errors.append(
                f"goal {g.idgoal} '{g.name}' maps to T goal {result[g.idgoal]}, but "
                f"archives that move store it as goal {g.idgoal}; T's goal "
                f"{g.idgoal} is '{by_id[g.idgoal].name if g.idgoal in by_id else '-'}'")
    return result, create, errors


def map_dims(src: list[Dimension], tgt: list[Dimension], overrides: dict[int, int],
             archive_needs_identity: bool, free_slots: dict[str, list[int]]
             ) -> tuple[dict[int, int], list[Dimension], list[str]]:
    """free_slots: per scope, column indexes that exist in the log tables and no T
    dimension uses (new T dimensions are placed there)."""
    by_id = {d.iddim: d for d in tgt}
    by_key: dict[tuple[str, str], list[Dimension]] = {}
    for d in tgt:
        by_key.setdefault((d.scope, _norm(d.name)), []).append(d)
    slots = {k: list(v) for k, v in free_slots.items()}
    result: dict[int, int] = {}
    create: list[Dimension] = []
    errors: list[str] = []
    next_id = max([d.iddim for d in tgt] + [d.iddim for d in src] + [0]) + 1
    for d in sorted(src, key=lambda d: d.iddim):
        match = by_key.get((d.scope, _norm(d.name)), [])
        if d.iddim in overrides:
            t = by_id.get(overrides[d.iddim])
            if t is None or t.scope != d.scope:
                errors.append(f"--dimension {d.iddim}={overrides[d.iddim]}: T has no "
                              f"{d.scope} dimension with that ID")
                continue
            result[d.iddim] = t.iddim
        elif len(match) == 1:
            result[d.iddim] = match[0].iddim
        elif len(match) > 1:
            errors.append(f"dimension {d.iddim} '{d.name}': ambiguous in T")
            continue
        else:
            if not slots.get(d.scope):
                errors.append(f"dimension {d.iddim} '{d.name}': no free {d.scope} slot "
                              f"in T (add one with ./console customdimensions:add-custom-dimension)")
                continue
            iddim = d.iddim if d.iddim not in by_id else next_id
            if iddim == next_id:
                next_id += 1
            new = Dimension(iddim, d.name, d.scope, slots[d.scope].pop(0))
            result[d.iddim] = new.iddim
            create.append(new)
        if archive_needs_identity and result.get(d.iddim) != d.iddim:
            errors.append(f"dimension {d.iddim} '{d.name}' maps to T dimension "
                          f"{result[d.iddim]}, but archives that move store it as {d.iddim}")
    return result, create, errors


def column_moves(src: list[Dimension], tgt_all: list[Dimension], dims: dict[int, int]
                 ) -> dict[str, dict[int, int]]:
    """Per scope: S column index -> T column index, for the raw log rows."""
    t_index = {d.iddim: d for d in tgt_all}
    out: dict[str, dict[int, int]] = {"visit": {}, "action": {}}
    for d in src:
        if d.iddim in dims:
            out[d.scope][d.index] = t_index[dims[d.iddim]].index
    return out
