import re
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts" / "swarm_ledger"
sys.path.insert(0, str(SCRIPTS))
import ledger_core as core  # noqa: E402
import ledger_server as server  # noqa: E402
import new_ledger  # noqa: E402

LITERAL = re.compile(r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(|(?<![-\w])(white|black)(?![-\w])")
ROLES = ("canvas", "surface-1", "surface-2", "overlay", "text", "muted", "dim", "rule", "edge")
HUES = ("accent", "positive", "warn", "destructive")
DOC = {"title": "Design", "overview": "o", "sources": [], "phases": [], "questions": [], "followups": []}


def palette():
    return (SCRIPTS / "palette.css").read_text(encoding="utf-8")


def css_of(page):
    return "\n".join(re.findall(r"<style>(.*?)</style>", page, re.S))


class Palette(unittest.TestCase):
    def test_palette_has_a_ramp_layer_and_a_role_layer_pointing_at_it(self):
        tokens = dict(re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", palette()))
        ramps = {k: v for k, v in tokens.items() if re.fullmatch(r"--[a-z]+-\d{2,3}", k)}
        self.assertGreaterEqual(len(ramps), 8)
        for name in ROLES + HUES:
            self.assertIn(f"--{name}", tokens)
            self.assertRegex(tokens[f"--{name}"], r"^var\(--[a-z]+-\d{2,3}\)$", name)

    def test_the_palette_keeps_the_ledger_colours(self):
        tokens = dict(re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", palette()))

        def value(role):
            return tokens[tokens[role][len("var(") : -1]]

        expected = {"--canvas": "#03050b", "--accent": "#3b82f6", "--signal": "#ef4444", "--destructive": "#ef4444"}
        for role, colour in expected.items():
            self.assertEqual(value(role), colour, role)
        self.assertEqual(value("--warn"), "#facc15")
        self.assertEqual(value("--positive"), "#4ade80")

    def test_the_palette_is_the_only_place_a_colour_value_appears(self):
        template = (SCRIPTS / "template.html").read_text(encoding="utf-8")
        self.assertIsNone(LITERAL.search(template), LITERAL.search(template))
        self.assertIsNone(LITERAL.search(server.HOME_STYLE))

    def test_palette_is_packaged_with_the_template(self):
        data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertIn("*.css", data["tool"]["setuptools"]["package-data"]["scripts.swarm_ledger"])


class PaletteReachesEveryPage(unittest.TestCase):
    def test_a_rendered_ledger_page_carries_the_palette(self):
        page = new_ledger.render(new_ledger.build_doc(DOC), "design-2026-01-01", 8765)
        self.assertIn(palette().strip(), css_of(page))
        self.assertNotIn("__LEDGER_PALETTE__", page)

    def test_home_and_bin_carry_the_palette(self):
        for view in ("home", "bin"):
            self.assertIn(palette().strip(), css_of(server.index_page(view)))

    def test_a_palette_edit_changes_the_page_version(self):
        before = core.page_version()
        with tempfile.TemporaryDirectory() as tmp:
            edited = Path(tmp) / "palette.css"
            edited.write_text(palette().replace("--accent:", "--accent: var(--red-500); --was:"), encoding="utf-8")
            with mock.patch.object(core, "PALETTE", edited):
                self.assertNotEqual(core.page_version(), before)


class SurfaceLadder(unittest.TestCase):
    def setUp(self):
        self.pages = {
            "ledger": css_of((SCRIPTS / "template.html").read_text(encoding="utf-8")),
            "home": server.HOME_STYLE,
        }

    def test_panels_sit_on_the_surface_tokens_over_one_canvas(self):
        ledger = self.pages["ledger"]
        self.assertRegex(re.search(r"(?m)^section \{([^}]*)\}", ledger).group(1), r"background: var\(--surface-1\)")
        self.assertRegex(re.search(r"(?m)^body \{([^}]*)\}", ledger).group(1), r"background: var\(--canvas\)")
        self.assertRegex(re.search(r"body\{([^}]*)\}", self.pages["home"]).group(1), r"background:var\(--canvas\)")

    def test_no_drop_shadows_only_glow(self):
        for name, css in self.pages.items():
            for value in re.findall(r"box-shadow:\s*([^;}]+)", css):
                for layer in value.split(","):
                    if layer.strip() in ("none", "inherit"):
                        continue
                    offsets = re.findall(r"-?[\d.]+(?:px)?", layer)[:2]
                    self.assertTrue(layer.strip().startswith("inset") or offsets == ["0", "0"], (name, layer))

    def test_no_capsule_badges(self):
        ledger = self.pages["ledger"]
        for selector in (".sync-badge", ".status"):
            rule = re.search(rf"(?m)^{re.escape(selector)} \{{([^}}]*)\}}", ledger).group(1)
            self.assertNotRegex(rule, r"(?<![-\w])background:", selector)
            self.assertNotRegex(rule, r"(?<![-\w])border:", selector)


if __name__ == "__main__":
    unittest.main()
