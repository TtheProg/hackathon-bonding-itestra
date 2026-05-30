from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from data_structures import Coord, TeamName


@dataclass
class ActiveEffectInfo:
    effect: str
    remaining_ticks: int


@dataclass
class SnakeInfo:
    body: List[Coord]
    alive: bool
    inventory: List[str]
    active_effects: List[ActiveEffectInfo]
    color: Optional[Tuple[int, int, int]] = None

    @property
    def head(self) -> Optional[Coord]:
        return self.body[0] if self.body else None

    @property
    def length(self) -> int:
        return len(self.body)


@dataclass
class Item:
    pos: Coord
    kind: str


def _coord(raw) -> Coord:
    return (int(raw[0]), int(raw[1]))


def fill_info_from_source(source) -> Dict[TeamName, SnakeInfo]:
    snakes = {}
    for team, info in source.items():
        snakes[team] = SnakeInfo(
            body=[_coord(c) for c in info.get("body", [])],
            alive=bool(info.get("alive", True)),
            inventory=list(info.get("inventory", [])),
            active_effects=[
                ActiveEffectInfo(
                    effect=e.get("effect", ""),
                    remaining_ticks=int(e.get("remaining_ticks", 0)),
                )
                for e in info.get("active_effects", [])
            ],
            color=tuple(info["color"]) if info.get("color") else None,
        )
    return snakes


def parse_items(raw_items) -> List[Item]:
    """Server sends items as a list of [[x, y], "Kind"] pairs."""
    items: List[Item] = []
    for entry in raw_items or []:
        try:
            if isinstance(entry, dict):
                pos = _coord(entry.get("position") or entry.get("pos"))
                kind = entry.get("kind") or entry.get("type") or "Apple"
            else:
                pos = _coord(entry[0])
                kind = str(entry[1]) if len(entry) > 1 else "Apple"
            items.append(Item(pos=pos, kind=kind))
        except (KeyError, IndexError, TypeError):
            continue
    return items


@dataclass
class Field:
    size: Tuple[int, int]
    snakes: Dict[TeamName, SnakeInfo]
    items: List[Item] = field(default_factory=list)

    @staticmethod
    def from_dict(raw: dict) -> "Field":
        size = tuple(int(v) for v in raw["size"])
        source = raw.get("snake") or raw.get("snakes") or {}
        snakes = fill_info_from_source(source)
        items = parse_items(raw.get("items"))
        return Field(size=size, snakes=snakes, items=items)

    def apples(self) -> List[Coord]:
        return [it.pos for it in self.items if it.kind == "Apple"]

    def hazards(self) -> List[Coord]:
        """Non-apple items we'd rather not eat (e.g. BadApple)."""
        return [it.pos for it in self.items if it.kind != "Apple"]
