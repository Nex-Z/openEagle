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

    def test_default_workspace_root_is_user_home(self) -> None:
        # 仅移除工作区根目录环境变量，保留 HOME/USERPROFILE 以便 Path.home() 正常解析
        env = {k: v for k, v in os.environ.items() if k != OPEN_EAGLE_WORKSPACE_ROOT_ENV}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(resolve_workspace_root(), Path.home())


if __name__ == "__main__":
    unittest.main()
