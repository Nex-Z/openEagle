from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


IMCommandName = Literal[
    "auto",
    "solo",
    "pause",
    "resume",
    "stop",
    "allow",
    "reject",
    "help",
]


@dataclass(frozen=True)
class IMCommand:
    name: IMCommandName
    argument: str = ""


_CONTROL_COMMANDS: dict[str, IMCommandName] = {
    "solo": "solo",
    "pause": "pause",
    "resume": "resume",
    "stop": "stop",
    "allow": "allow",
    "reject": "reject",
    "help": "help",
}


def parse_im_command(text: str, *, allow_empty_task: bool = False) -> IMCommand:
    stripped = text.strip()
    if not stripped:
        return IMCommand(name="help")
    if not stripped.startswith("/"):
        return IMCommand(name="auto", argument=stripped)

    head, _, tail = stripped[1:].partition(" ")
    command = _CONTROL_COMMANDS.get(head.lower())
    if command is None:
        return IMCommand(name="help")

    if command == "solo" and not tail.strip() and not allow_empty_task:
        return IMCommand(name="help")

    return IMCommand(name=command, argument=tail.strip())
