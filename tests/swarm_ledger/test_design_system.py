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

from scripts.swarm_ledger import page_assets  # noqa: E402
from tests.swarm_ledger.ledger_page import page_source, rendered_home  # noqa: E402
from tests.swarm_ledger.test_swarm_layout import NOW_MS, browser, open_page, status  # noqa: E402, F401

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


def home_page_source():
    page = server.index_page()
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

        expected = {"--canvas": "#050812", "--accent": "#3b82f6", "--signal": "#ef4444", "--destructive": "#ef4444"}
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

    def test_the_home_and_bin_shell_carries_the_palette(self):
        self.assertIn(palette().strip(), css_of(home_page_source()))

    def test_a_palette_edit_changes_the_page_version(self):
        before = core.page_version()
        with tempfile.TemporaryDirectory() as tmp:
            edited = Path(tmp) / "palette.css"
            edited.write_text(palette().replace("--accent:", "--accent: var(--red-500); --was:"), encoding="utf-8")
            with mock.patch.object(page_assets, "PALETTE", edited):
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


RECORD_ROWS = (".prio", ".item", ".stat")
TILE_PROPS = ("background", "background-color", "border-radius", "box-shadow", "border")
ROW_STATES = (":hover", ":focus-visible", ":focus-within")


def tiled_rows(css):
    """Record rows and sibling-spaced rows that paint their own surface instead of a hairline."""
    found = []
    rules = re.findall(r"([^{}]+)\{([^{}]*)\}", css)
    gapped = {m.group(1) for heads, _ in rules for m in re.finditer(r"(\.[\w-]+)\s*\+\s*\1\b", heads)}
    for heads, body in rules:
        props = {line.split(":", 1)[0].strip() for line in body.split(";") if ":" in line}
        if not props & set(TILE_PROPS):
            continue
        for part in heads.split(","):
            last = re.split(r"[\s>+~]+", part.strip())[-1]
            if any(state in last for state in ROW_STATES):
                continue
            row = re.match(r"\.[\w-]+", last)
            if row and (row.group(0) in RECORD_ROWS or row.group(0) in gapped) and row.group(0) not in found:
                found.append(row.group(0))
    return found


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
            "--ink-950": "#050812",
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
            "--canvas": "#050812",
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

    def test_the_static_ledger_and_home_shells_carry_no_main_class(self):
        mains = {
            "ledger": re.search(r"<main([^>]*)>", page_source()).group(1),
            "home": re.search(r"<main([^>]*)>", server.index_page()).group(1),
        }
        self.assertEqual(mains, {"ledger": "", "home": ""})

    def test_priority_item_and_stat_rows_are_hairline_rows(self):
        css = css_of(page_source())
        for row in RECORD_ROWS:
            self.assertEqual(declared(css, row).get("border-top"), "1px solid var(--rule)", row)
            self.assertEqual(declared(css, f"{row}:first-child").get("border-top"), "0", row)

    def test_no_record_row_is_painted_as_a_tile(self):
        self.assertEqual(tiled_rows(css_of(page_source()) + home_style()), [])

    def test_the_tile_check_turns_red_on_a_planted_tile(self):
        clean = css_of(page_source())
        planted = {
            ".prio": ".prio { background: var(--surface-2); border-radius: 6px; }",
            ".item": "#ledger .item { padding: 12px 14px; background: var(--surface-1); }",
            ".stat": ".stat { box-shadow: 0 0 0 1px var(--edge); }",
            ".note-row": ".note-row { background: var(--surface-2); }\n.note-row + .note-row { margin-top: 6px; }",
        }
        for row, rule in planted.items():
            self.assertIn(row, tiled_rows(clean + "\n" + rule), row)

    def test_stat_labels_sit_on_a_fixed_track(self):
        columns = declared(css_of(page_source()), "#stats .stat")["grid-template-columns"]
        self.assertRegex(columns, r"^\d+px minmax\(0, 1fr\)$")

    def test_stat_values_stay_on_one_line(self):
        value = declared(css_of(page_source()), ".stat dd")
        self.assertEqual(
            (value.get("white-space"), value.get("overflow"), value.get("text-overflow")),
            ("nowrap", "hidden", "ellipsis"),
        )


def test_every_overflowing_agent_cell_ellipsizes_and_exposes_its_full_text(open_page):  # noqa: F811
    payload = status()
    agent = payload["agents"][1]
    agent.update(
        profile="engineer with a long profile",
        overlays=["brain", "review", "research"],
        promoted=True,
        capabilities={"push_inbox": False, "native_terminal_attach": True},
    )
    payload["agents"][0]["remote_control"] = {"state": "live", "detail": "Remote session with complete detail"}
    payload["agents"][0]["capabilities"] = {"push_inbox": True, "native_terminal_attach": False}
    payload["executions"] = {
        agent["name"]: {
            "backend": "kubernetes",
            "generation": 2,
            "pool": "standard worker pool",
            "observed": "working",
            "seen_at": NOW_MS - 120_000,
            "terminal_behind": True,
        }
    }
    payload["terminals"] = {
        agent["name"]: {
            "state": "live",
            "checked_at": NOW_MS - 300_000,
            "live_at": NOW_MS - 300_000,
            "fresh_ms": 30_000,
            "reason": "connection_unavailable",
        }
    }
    page = open_page(payload, width=1920, clock=True)
    page.tab.set_viewport_size({"width": 1920, "height": 1080})
    page.tab.evaluate("""() => {
        document.querySelectorAll('#agents-table th').forEach(th => th.style.width = '48px');
        const table = document.querySelector('#agents-table');
        table.style.width = '576px';
        table.style.minWidth = '0';
    }""")
    found = page.tab.eval_on_selector_all(
        "#swarm-agents tr:not(.page-more) td:not(:has(> .sw-acts))",
        """cells => cells.filter(c => c.textContent.trim()).map(c => {
            const style = getComputedStyle(c);
            const tips = [c, ...c.querySelectorAll('*')].map(el => window.ledgerTip(el));
            return {text: c.innerText, tips, overflow: style.overflow, ellipsis: style.textOverflow};
        })""",
    )
    for cell in found:
        assert cell["overflow"] == "hidden", cell
        assert cell["ellipsis"] == "ellipsis", cell
        assert cell["tips"][0], cell
        assert all(tip == cell["tips"][0] for tip in cell["tips"]), cell
        for word in cell["text"].lower().split():
            assert word in cell["tips"][0].lower(), cell
    assert any("standard worker pool" in cell["tips"][0] for cell in found)
    assert any("connection unavailable" in cell["tips"][0] for cell in found)
    assert any(
        "Remote session with complete detail" in cell["tips"][0] and "no native terminal" in cell["tips"][0]
        for cell in found
    )
    nested = page.tab.eval_on_selector_all(
        "#swarm-agents :is(.sw-transport, .sw-promoted, .sw-sub)",
        """els => els.map(el => {
            const style = getComputedStyle(el);
            return [el.className, style.display, style.overflow, style.textOverflow];
        })""",
    )
    for name, display, overflow, ellipsis in nested:
        if name == "sw-sub":
            assert (overflow, ellipsis) == ("hidden", "ellipsis")
        else:
            assert display == "inline", name
    execution = page.tab.locator("#swarm-agents tr").nth(1).locator("td").nth(8)
    execution.scroll_into_view_if_needed()
    page.tab.clock.run_for(100)
    execution.hover()
    page.tab.clock.run_for(1100)
    assert page.tab.locator(".ledger-tip:popover-open").inner_text() == execution.get_attribute("data-tip")


def test_the_home_script_scopes_home_and_bin_on_main(browser):  # noqa: F811
    for view in ("home", "bin"):
        with mock.patch.object(server, f"{view}_summaries", return_value=[{"slug": "a", "closed_at": 0}]):
            page = rendered_home(server, browser, view)
        assert re.search(r"<main([^>]*)>", page).group(1) == f' class="{view}"'


if __name__ == "__main__":
    unittest.main()
