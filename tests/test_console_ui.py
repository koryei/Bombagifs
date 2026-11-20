"""Terminal UI rendering (needs rich, so it runs in .venv-selfbot)."""

from __future__ import annotations

import unittest
from importlib.util import find_spec

HAS_RICH = find_spec("rich") is not None

if HAS_RICH:
    import console_ui


def render(renderable: object, width: int = 100) -> str:
    console = console_ui.create_console(record=True, width=width, force_terminal=False, color_system=None)
    console.print(renderable)
    return console.export_text()


@unittest.skipUnless(HAS_RICH, "needs rich (run in .venv-selfbot)")
class BootBannerTests(unittest.TestCase):
    INFO = {"account": "Bombaclat#0001", "zipline": "gifs.example.com", "auto-accept friends": "off"}

    def test_banner_shows_logo_title_and_info_rows(self) -> None:
        text = render(console_ui.render_boot_banner(self.INFO))
        self.assertIn("/ __ )/ __ \\/  |/  / __ )/   | / ____/  _/ ____/", text)  # the logo
        self.assertIn("Bombagif", text)
        for label, value in self.INFO.items():
            self.assertIn(label, text)
            self.assertIn(value, text)

    LOGO_LINE = "/ __ )/ __ \\/  |/  / __ )/   | / ____/  _/ ____/"

    def test_wide_terminal_puts_logo_and_facts_side_by_side(self) -> None:
        text = render(console_ui.render_boot_banner(self.INFO), width=100)
        row = next(line for line in text.splitlines() if self.LOGO_LINE in line)
        self.assertIn("zipline", row)

    def test_medium_terminal_stacks_facts_under_the_logo(self) -> None:
        text = render(console_ui.render_boot_banner(self.INFO), width=70)
        self.assertIn(self.LOGO_LINE, text)
        self.assertIn("Bombaclat#0001", text)
        self.assertIn("gifs.example.com", text)
        self.assertTrue(all("zipline" not in line for line in text.splitlines() if self.LOGO_LINE in line))

    def test_narrow_terminal_drops_the_logo_and_keeps_full_values(self) -> None:
        text = render(console_ui.render_boot_banner(self.INFO), width=50)
        self.assertNotIn("/ __ )", text)
        self.assertIn("Bombaclat#0001", text)
        self.assertIn("gifs.example.com", text)

    def test_create_console_uses_the_theme(self) -> None:
        console = console_ui.create_console()
        self.assertTrue(console.get_style("brand").bold)
        self.assertTrue(console.get_style("bad").bold)


if __name__ == "__main__":
    unittest.main()
