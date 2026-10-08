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

from tests.swarm_ledger.ledger_page import page_source  # noqa: E402

LITERAL = re.compile(r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(|(?<![-\w])(white|black)(?![-\w])")
ROLES = ("canvas", "surface-1", "surface-2", "overlay", "text", "muted", "dim", "rule", "edge")
HUES = ("accent", "positive", "warn", "destructive")


def palette():
    return (SCRIPTS / "palette.css").read_text(encoding="utf-8")


def css_of(page):
    return "\n".join(re.findall(r"<style>(.*?)</style>", page, re.S))


def home_style():
    static = core.static_assets()
    css = static["css/home.css"].read_text(encoding="utf-8") + static["css/tooltips.css"].read_text(encoding="utf-8")
    return css.replace("\n", "")


def home_page_source(view):
    page = server.index_page(view)
    version = server.served_version()
    for name, path in core.static_assets().items():
        href = f'<link rel="stylesheet" href="/static/{version}/{name}">'
        page = page.replace(href, f"<style>\n{path.read_text(encoding='utf-8')}</style>")
    return page


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

        expected = {"--canvas": "#010104", "--accent": "#3b82f6", "--signal": "#ef4444", "--destructive": "#ef4444"}
        for role, colour in expected.items():
            self.assertEqual(value(role), colour, role)
        self.assertEqual(value("--warn"), "#facc15")
        self.assertEqual(value("--positive"), "#4ade80")

    def test_the_palette_is_the_only_place_a_colour_value_appears(self):
        template = page_source().replace(f"<style>\n{palette()}</style>", "")
        self.assertIsNone(LITERAL.search(template), LITERAL.search(template))
        self.assertIsNone(LITERAL.search(home_style()))

    def test_palette_is_packaged_with_the_template(self):
        data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertIn("*.css", data["tool"]["setuptools"]["package-data"]["scripts.swarm_ledger"])


class PaletteReachesEveryPage(unittest.TestCase):
    def test_a_rendered_ledger_page_carries_the_palette(self):
        page = page_source()
        self.assertIn(palette().strip(), css_of(page))
        self.assertNotIn("__LEDGER_PALETTE__", page)

    def test_home_and_bin_carry_the_palette(self):
        for view in ("home", "bin"):
            self.assertIn(palette().strip(), css_of(home_page_source(view)))

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
            "ledger": css_of(page_source()),
            "home": home_style(),
        }

    def test_panels_sit_on_the_surface_tokens_over_one_canvas(self):
        ledger = self.pages["ledger"]
        self.assertRegex(re.search(r"(?m)^section \{([^}]*)\}", ledger).group(1), r"background: var\(--surface-1\)")
        self.assertRegex(re.search(r"(?m)^body \{([^}]*)\}", ledger).group(1), r"background: var\(--canvas\)")
        self.assertRegex(re.search(r"body\{([^}]*)\}", self.pages["home"]).group(1), r"background:var\(--canvas\)")

    def test_drop_shadows_come_only_from_the_lift_and_spill_roles(self):
        for name, css in self.pages.items():
            for value in re.findall(r"box-shadow:\s*([^;}]+)", css):
                for layer in value.split(","):
                    if layer.strip() in ("none", "inherit"):
                        continue
                    if layer.strip().startswith("var("):
                        self.assertIn(layer.strip(), ("var(--panel-lift)", "var(--tab-spill)"), (name, layer))
                        continue
                    offsets = re.findall(r"-?[\d.]+(?:px)?", layer)[:2]
                    self.assertTrue(layer.strip().startswith("inset") or offsets == ["0", "0"], (name, layer))

    def test_nothing_glows(self):
        for name, css in self.pages.items():
            self.assertNotIn("text-shadow", css, name)
            self.assertNotIn("drop-shadow(", css, name)
            self.assertNotIn("--glow", css, name)
            for value in re.findall(r"box-shadow:\s*([^;}]+)", css):
                for layer in value.split(","):
                    self.assertFalse(re.match(r"\s*0\s+0\s+[\d.]+px", layer), (name, layer))

    def test_the_canvas_carries_the_indigo_top_wash_and_the_blue_corner_wash(self):
        tokens = dict(re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", palette()))
        washes = [w.strip() for w in re.split(r",\s*(?=radial-gradient)", tokens["--backdrop"])]
        self.assertEqual(
            washes,
            [
                "radial-gradient(ellipse 70% 45% at 50% -8%, var(--indigo-600-012), transparent 70%)",
                "radial-gradient(ellipse 40% 30% at 100% 0%, var(--blue-500-006), transparent 75%)",
            ],
        )
        self.assertEqual(tokens["--indigo-600-012"], "rgba(58, 61, 238, .12)")
        self.assertEqual(tokens["--blue-500-006"], "rgba(59, 130, 246, .06)")

    def test_no_capsule_badges(self):
        ledger = self.pages["ledger"]
        for selector in (".sync-badge", ".status"):
            rule = re.search(rf"(?m)^{re.escape(selector)} \{{([^}}]*)\}}", ledger).group(1)
            self.assertNotRegex(rule, r"(?<![-\w])background:", selector)
            self.assertNotRegex(rule, r"(?<![-\w])border:", selector)


def block(selector):
    body = re.search(rf"(?m)^{re.escape(selector)} \{{([^}}]*)\}}", palette()).group(1)
    return dict(re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", body))


def tokens():
    return block(":root")


def resolve(name):
    found = tokens()
    value = found[name].strip()
    while value.startswith("var(") and value.endswith(")") and value[4:-1] in found:
        value = found[value[4:-1]].strip()
    return value


def declared(css, selector):
    merged = {}
    for heads, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        if selector in (part.strip() for part in heads.split(",")):
            for line in body.split(";"):
                if ":" in line:
                    prop, value = line.split(":", 1)
                    merged[prop.strip()] = value.strip()
    return merged


class BluePalette(unittest.TestCase):
    def setUp(self):
        self.ledger = css_of(page_source())
        self.home = home_style()

    def test_the_ramp_holds_the_approved_blue_values(self):
        expected = {
            "--ink-950": "#010104",
            "--steel-060": "#0e131b",
            "--steel-100": "#151c29",
            "--steel-160": "#1b2636",
            "--steel-200": "#222f44",
            "--indigo-300": "#9188dd",
            "--indigo-600": "#3a3dee",
            "--blue-600": "#155dfc",
        }
        for name, colour in expected.items():
            self.assertEqual(tokens()[name], colour, name)

    def test_roles_take_the_blue_ramp_instead_of_white_lifts(self):
        expected = {
            "--canvas": "#010104",
            "--veil": "#0e131b",
            "--hover": "#151c29",
            "--rule": "#151c29",
            "--surface-2": "#1b2636",
            "--edge": "#222f44",
            "--tab-active": "#9188dd",
            "--ring": "#155dfc",
            "--data-line": "#3a3dee",
            "--data-fill-start": "rgba(58, 61, 238, .30)",
            "--data-fill-end": "rgba(58, 61, 238, .04)",
            "--surface-1": "transparent",
        }
        for role, colour in expected.items():
            self.assertEqual(resolve(role), colour, role)
        self.assertNotIn("255, 255, 255", palette())

    def test_the_ledger_keeps_its_state_colours(self):
        expected = {
            "--positive": "#4ade80",
            "--warn": "#facc15",
            "--signal": "#ef4444",
            "--destructive": "#ef4444",
            "--accent": "#3b82f6",
        }
        for role, colour in expected.items():
            self.assertEqual(resolve(role), colour, role)

    def test_panels_are_frameless_at_rest_and_take_the_blue_edge_and_lift_on_hover(self):
        self.assertEqual(resolve("--panel-edge"), "rgba(147, 197, 253, .10)")
        self.assertEqual(
            tokens()["--panel-lift"],
            "inset 0 1px 0 var(--blue-300-010), 0 10px 30px -14px var(--blue-500-035), 0 0 0 1px var(--blue-500-018)",
        )
        panels = {"ledger": ("section", "#swarm-box"), "home": (".home .panel",)}
        for page, selectors in panels.items():
            css = getattr(self, page)
            for selector in selectors:
                rest, hover = declared(css, selector), declared(css, f"{selector}:hover")
                self.assertEqual(rest.get("outline"), "1px solid transparent", selector)
                self.assertEqual(rest.get("outline-offset"), "-1px", selector)
                self.assertNotIn("border", rest, selector)
                self.assertEqual(hover.get("outline-color"), "var(--panel-edge)", selector)
                self.assertEqual(hover.get("box-shadow"), "var(--panel-lift)", selector)

    def test_the_active_tab_takes_the_indigo_underline_and_its_spill(self):
        active = declared(self.ledger, '.tab[aria-selected="true"]')
        self.assertEqual(active.get("color"), "var(--tab-active)")
        self.assertEqual(active.get("border-bottom-color"), "var(--tab-active)")
        self.assertEqual(active.get("box-shadow"), "var(--tab-spill)")
        self.assertEqual(tokens()["--tab-spill"], "0 6px 12px -8px var(--indigo-300-055)")
        self.assertEqual(tokens()["--indigo-300-055"], "rgba(145, 136, 221, .55)")

    def test_rows_hover_on_the_hover_step(self):
        self.assertEqual(declared(self.ledger, ".sw-table tbody tr:hover td").get("background"), "var(--hover)")
        self.assertEqual(declared(self.home, "li.row:hover").get("background"), "var(--hover)")

    def test_focus_takes_the_blue_ring(self):
        self.assertEqual(declared(self.ledger, ":focus-visible").get("outline"), "2px solid var(--ring)")
        for name, css in (("ledger", self.ledger), ("home", self.home)):
            for value in re.findall(r"outline:\s*([^;}]+)", css):
                self.assertNotRegex(value, r"var\(--(?:accent|accent-2|link)\)", name)

    def test_progress_bars_take_the_data_fill_and_line(self):
        fill = declared(self.ledger, ".bar > i")
        self.assertEqual(fill.get("background"), "linear-gradient(90deg, var(--data-fill-start), var(--data-fill-end))")
        self.assertEqual(fill.get("border-right"), "2.5px solid var(--data-line)")

    def test_no_text_glow_anywhere(self):
        for name, css in (("palette", palette()), ("ledger", self.ledger), ("home", self.home)):
            self.assertNotIn("text-shadow", css, name)


class LandingBays(unittest.TestCase):
    def test_ledger_and_bin_sections_take_the_landing_bay_and_inner_tiles_the_raised_bay(self):
        scoped = block("main:not(.home)")
        self.assertEqual(set(scoped), {"--surface-1", "--surface-2"})
        self.assertEqual(scoped["--surface-1"], "var(--bay-050)")
        self.assertEqual(scoped["--surface-2"], "var(--bay-075)")
        self.assertEqual(tokens()["--bay-050"], "rgba(147, 197, 253, .05)")
        self.assertEqual(tokens()["--bay-075"], "rgba(147, 197, 253, .075)")

    def test_home_keeps_its_frameless_surfaces(self):
        self.assertEqual(resolve("--surface-1"), "transparent")
        self.assertEqual(resolve("--surface-2"), "#1b2636")

    def test_only_home_falls_outside_the_bay_scope(self):
        mains = {
            "ledger": re.search(r"<main([^>]*)>", page_source()).group(1),
            "home": re.search(r"<main([^>]*)>", server.index_page("home")).group(1),
            "bin": re.search(r"<main([^>]*)>", server.index_page("bin")).group(1),
        }
        self.assertEqual(mains, {"ledger": "", "home": ' class="home"', "bin": ' class="bin"'})


if __name__ == "__main__":
    unittest.main()
