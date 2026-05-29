from __future__ import annotations

import os
from pathlib import Path

OPEN_EAGLE_WORKSPACE_ROOT_ENV = "OPEN_EAGLE_WORKSPACE_ROOT"


def resolve_workspace_root() -> Path:
    configured = os.environ.get(OPEN_EAGLE_WORKSPACE_ROOT_ENV)
    if configured:
        return Path(configured).expanduser().resolve()
    return Path.home()
