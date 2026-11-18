"""The shared status words and shapes (sfactory/status.py): a state is never told by colour alone."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "factory"))

from sfactory import status  # noqa: E402


class StatusTest(unittest.TestCase):
    def test_every_state_has_a_word_a_glyph_and_an_icon(self):
        for s in status.STATUSES.values():
            self.assertTrue(s.word and s.glyph and "<" in s.icon, s.key)
            self.assertIn(s.tone, (status.NEUTRAL, status.INFO, status.ATTENTION, status.NEGATIVE))

    def test_states_in_the_same_tone_still_differ_by_shape_or_word(self):
        seen: dict[tuple[str, str], str] = {}
        for s in status.STATUSES.values():
            self.assertNotIn((s.glyph, s.word), seen, s.key)
            seen[(s.glyph, s.word)] = s.key

    def test_run_outcomes_and_budget_states_are_covered(self):
        for key in ("done", "failed", "interrupted", "skipped", "running", "active", "paused",
                    "within", "approaching", "near", "over"):  # engine.models.Outcome and F-33's budget states
            self.assertIn(key, status.STATUSES)
        self.assertNotEqual(status.status("done").glyph, status.status("failed").glyph)

    def test_a_mark_carries_icon_and_escaped_word(self):
        html = status.mark("failed", "<script>alert(1)</script>")
        self.assertIn('class="mark negative"', html)
        self.assertIn('data-state="failed"', html)
        self.assertIn("<svg", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("<script>", html)

    def test_an_unknown_state_reads_as_unknown(self):
        self.assertEqual(status.status("exploded").key, "unknown")
        self.assertEqual(status.text("exploded"), "? Unknown")
        self.assertEqual(status.text("paused", "Paused after 3 failures"), "‖ Paused after 3 failures")

    def test_the_stylesheet_styles_every_tone_in_light_and_dark(self):
        for tone in (status.NEUTRAL, status.INFO, status.ATTENTION, status.NEGATIVE):
            self.assertIn(f".mark.{tone}", status.CSS)
        self.assertIn("prefers-color-scheme:dark", status.CSS)


if __name__ == "__main__":
    unittest.main()
