from typing import List, Tuple, Literal, get_args

Direction = Literal["NORTH", "SOUTH", "EAST", "WEST"]
ItemKind = Literal["will be shown later"]
TeamName = str
Coord = Tuple[int, int]


def get_directions_as_list() -> List[Direction]:
    return list(get_args(Direction))
