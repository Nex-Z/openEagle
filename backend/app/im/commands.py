from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


IMCommandName = Literal[
    "chat",
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


def parse_im_command(text: str) -> IMCommand:
    stripped = text.strip()
    if not stripped.startswith("/"):
        return IMCommand(name="chat", argument=stripped)

    head, _, tail = stripped[1:].partition(" ")
    command = _CONTROL_COMMANDS.get(head.lower())
    if command is None:
        return IMCommand(name="chat", argument=stripped)

    if command == "solo" and not tail.strip():
        return IMCommand(name="help")

    return IMCommand(name=command, argument=tail.strip())
