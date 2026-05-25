from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.paths import OPEN_EAGLE_WORKSPACE_ROOT_ENV, resolve_workspace_root


class WorkspaceRootTest(unittest.TestCase):
    def test_env_workspace_root_overrides_source_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {OPEN_EAGLE_WORKSPACE_ROOT_ENV: tmp}):
                self.assertEqual(resolve_workspace_root(), Path(tmp).resolve())

    def test_default_workspace_root_is_backend_parent(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(resolve_workspace_root(), Path(__file__).resolve().parents[2])


if __name__ == "__main__":
    unittest.main()
