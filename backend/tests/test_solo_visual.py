from __future__ import annotations

import unittest

from app.solo_visual import build_solo_step_visual, should_delay_for_visual


class SoloVisualTests(unittest.TestCase):
    def test_point_visual_resolves_normalized_coordinates(self) -> None:
        visual = build_solo_step_visual(
            "click",
            {"x": 0.5, "y": 0.25, "target": "search box"},
            display_text="Click the search box",
            screenshot_path="C:/tmp/screen.png",
            capture_region={"left": 100, "top": 50, "width": 800, "height": 600, "displayIndex": 2},
        )

        self.assertEqual(visual["kind"], "point")
        self.assertEqual(visual["x"], 500)
        self.assertEqual(visual["y"], 200)
        self.assertEqual(visual["screenshotX"], 400)
        self.assertEqual(visual["screenshotY"], 150)
        self.assertEqual(visual["screenshotWidth"], 800)
        self.assertEqual(visual["screenshotHeight"], 600)
        self.assertEqual(visual["displayIndex"], 2)
        self.assertEqual(visual["coordinateSpace"], "screen")
        self.assertEqual(visual["targetLabel"], "search box")
        self.assertEqual(visual["displayText"], "Click the search box")

    def test_point_visual_uses_monitor_offset_for_local_pixels(self) -> None:
        visual = build_solo_step_visual(
            "move_mouse",
            {"x": 240, "y": 120},
            capture_region={"left": -1600, "top": 20, "width": 1600, "height": 900, "displayIndex": 1},
        )

        self.assertEqual(visual["x"], -1360)
        self.assertEqual(visual["y"], 140)
        self.assertEqual(visual["screenshotX"], 240)
        self.assertEqual(visual["screenshotY"], 120)

    def test_point_visual_without_capture_region_has_no_marker_coordinates(self) -> None:
        visual = build_solo_step_visual("click", {"x": 0.5, "y": 0.5})

        self.assertEqual(visual["kind"], "point")
        self.assertNotIn("x", visual)
        self.assertNotIn("screenshotX", visual)
        self.assertTrue(should_delay_for_visual("click", {"x": 0.5, "y": 0.5}))

    def test_safe_args_preview_hides_text_and_redacts_commands(self) -> None:
        typed = build_solo_step_visual("type_text", {"text": "super secret"})
        self.assertEqual(typed["kind"], "keyboard")
        self.assertEqual(typed["safeArgsPreview"], {"text": "[hidden 12 chars]"})

        command = build_solo_step_visual(
            "execute_command",
            {
                "command": "python C:/Users/me/project/run.py --token=abc123",
                "cwd": "C:/Users/me/project",
            },
        )
        preview = command["safeArgsPreview"]
        self.assertEqual(command["kind"], "command")
        self.assertIn("[path]", preview["command"])
        self.assertIn("[hidden]", preview["command"])
        self.assertEqual(preview["cwd"], ".../project")

    def test_open_url_preview_drops_query_and_fragment(self) -> None:
        visual = build_solo_step_visual(
            "open_url",
            {"url": "https://example.com/path?q=secret#token"},
        )

        self.assertEqual(visual["kind"], "navigation")
        self.assertEqual(visual["safeArgsPreview"], {"url": "https://example.com/path"})


if __name__ == "__main__":
    unittest.main()
