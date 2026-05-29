from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.capability_files import (
    load_file_backed_settings,
    load_mcp_configs,
    load_skill_configs,
    save_file_backed_settings,
    save_skill_configs,
)


class CapabilityFilesTest(unittest.TestCase):
    def test_legacy_settings_are_materialized_to_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = {
                "agent": {"provider": "mock"},
                "mcp": [
                    {
                        "id": "mcp-1",
                        "name": "Filesystem",
                        "transport": "stdio",
                        "endpoint": "npx server .",
                        "description": "Files",
                        "enabled": True,
                    }
                ],
                "skills": [
                    {
                        "id": "skill-1",
                        "name": "Careful Reporter",
                        "description": "Reports carefully",
                        "prompt": "Always lead with the conclusion.",
                        "enabled": True,
                    }
                ],
            }

            loaded = load_file_backed_settings(root, legacy)

            self.assertIsNotNone(loaded)
            self.assertEqual(loaded["mcp"][0]["name"], "Filesystem")
            self.assertEqual(loaded["skills"][0]["name"], "Careful Reporter")
            self.assertTrue((root / ".open-eagle" / "mcp.json").exists())
            self.assertTrue((root / ".open-eagle" / "skills" / "careful-reporter" / "SKILL.md").exists())

            stripped = save_file_backed_settings(root, loaded)
            self.assertNotIn("mcp", stripped)
            self.assertNotIn("skills", stripped)
            self.assertEqual(stripped["agent"]["provider"], "mock")

    def test_claude_style_mcp_json_is_imported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / ".open-eagle" / "mcp.json"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "filesystem": {
                                "command": "npx",
                                "args": ["-y", "@modelcontextprotocol/server-filesystem", "."],
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )

            configs = load_mcp_configs(root)

            self.assertEqual(len(configs), 1)
            self.assertEqual(configs[0]["id"], "mcp-filesystem")
            self.assertEqual(configs[0]["name"], "filesystem")
            self.assertEqual(configs[0]["transport"], "stdio")
            self.assertEqual(configs[0]["endpoint"], "npx -y @modelcontextprotocol/server-filesystem .")

    def test_skill_front_matter_is_used_when_metadata_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill_dir = root / ".open-eagle" / "skills" / "v2ex-digest"
            skill_dir.mkdir(parents=True)
            skill_dir.joinpath("SKILL.md").write_text(
                "---\n"
                "name: v2ex-digest\n"
                "description: V2EX digest assistant\n"
                "---\n\n"
                "# V2EX Digest\n",
                encoding="utf-8",
            )

            skills = load_skill_configs(root)

            self.assertEqual(len(skills), 1)
            self.assertEqual(skills[0]["id"], "v2ex-digest")
            self.assertEqual(skills[0]["name"], "v2ex-digest")
            self.assertEqual(skills[0]["description"], "V2EX digest assistant")
            self.assertIn("# V2EX Digest", skills[0]["prompt"])

    def test_removed_skills_are_archived_not_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            save_skill_configs(
                root,
                [
                    {"id": "skill-1", "name": "One", "prompt": "one"},
                    {"id": "skill-2", "name": "Two", "prompt": "two"},
                ],
            )

            save_skill_configs(root, [{"id": "skill-2", "name": "Two", "prompt": "two"}])

            self.assertFalse((root / ".open-eagle" / "skills" / "one").exists())
            archived = list((root / ".open-eagle" / "deleted-skills").glob("one-*"))
            self.assertEqual(len(archived), 1)
            self.assertTrue((archived[0] / "SKILL.md").exists())


if __name__ == "__main__":
    unittest.main()
