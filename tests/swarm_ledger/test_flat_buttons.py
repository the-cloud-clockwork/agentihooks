import re
import unittest
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger" / "template.html"
STATES = re.compile(
    r":hover|:active|:focus|:disabled|:checked|::|\.armed|\.sending|\[aria-(?:pressed|selected|expanded)=\"true\"\]"
)
CLEAR = re.compile(r"^(?:none|transparent|0|inherit)$|\btransparent\b")


def page():
    return TEMPLATE.read_text(encoding="utf-8")


def css_rules(css):
    css, stack, buf, rules = re.sub(r"/\*.*?\*/", "", css, flags=re.S), [], "", []
    for ch in css:
        if ch == "{":
            stack.append(buf.strip())
            buf = ""
        elif ch == "}":
            head = stack.pop()
            if not head.startswith("@"):
                rules.append((tuple(stack), head, buf.strip()))
            buf = ""
        else:
            buf += ch
    return rules


def declarations(body):
    return {k.strip(): v.strip() for k, v in (d.split(":", 1) for d in body.split(";") if ":" in d)}


def button_classes(html):
    found = set()
    for attr in re.findall(r"<button\b[^>]*\bclass=\"([^\"]+)\"", html) + re.findall(
        r"h\(\"button\", \{ class: \"([^\"]+)\"", html
    ):
        found.update(attr.split())
    return found


def rest_selectors(selector, classes):
    for part in selector.split(","):
        last = part.strip().split()[-1]
        if STATES.search(last):
            continue
        tokens = set(re.findall(r"\.([\w-]+)", last))
        if last.startswith("button") or tokens & classes:
            yield part.strip()


class FlatButtons(unittest.TestCase):
    def test_the_page_names_its_buttons(self):
        classes = button_classes(page())
        for name in ("sw-btn", "sw-step", "sw-mode", "sync", "link", "tab", "mini-sync", "title-pen"):
            self.assertIn(name, classes)

    def test_no_button_has_a_fill_border_or_glass_at_rest(self):
        html = page()
        classes = button_classes(html)
        css = "\n".join(re.findall(r"<style>(.*?)</style>", html, re.S))
        checked = []
        for _media, selector, body in css_rules(css):
            for target in rest_selectors(selector, classes):
                checked.append(target)
                for prop, value in declarations(body).items():
                    if prop in ("background", "background-color", "background-image"):
                        self.assertRegex(value, CLEAR, f"{target} fills its {prop} at rest: {value}")
                    if prop in ("border", "border-color") or re.fullmatch(r"border-(?:top|right|bottom|left)", prop):
                        self.assertRegex(value, CLEAR, f"{target} draws a {prop} at rest: {value}")
                    if prop.endswith("backdrop-filter") or prop == "box-shadow":
                        self.assertRegex(value, r"^none$", f"{target} sets {prop} at rest: {value}")
        for name in (".sw-btn", ".sync", ".sw-btn.sw-step", ".sw-btn.sw-mode"):
            self.assertIn(name, checked)

    def test_hover_lifts_the_button_and_never_draws_a_frame(self):
        css = "\n".join(re.findall(r"<style>(.*?)</style>", page(), re.S))
        hovers = [(s, declarations(b)) for _m, s, b in css_rules(css) if re.search(r"\.(?:sw-btn|sync):hover", s)]
        self.assertTrue(hovers)
        for selector, decl in hovers:
            self.assertEqual(decl.get("background"), "var(--hover)", selector)
            self.assertNotIn("border-color", decl, selector)
            self.assertNotIn("box-shadow", decl, selector)


if __name__ == "__main__":
    unittest.main()
