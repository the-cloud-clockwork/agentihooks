import io
import re
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"))

from scripts.swarm_ledger import ledger_core as core  # noqa: E402
from scripts.swarm_ledger import ledger_server as server  # noqa: E402

LOGO_LINK = '<a class="logo-link" href="/" aria-label="HOME"><span class="logo" aria-hidden="true"></span></a>'
BRAND = f'<header>{LOGO_LINK}<span class="brand">agentihooks</span>'
WATERMARK = '<div class="watermark" aria-hidden="true"></div>'
LEDGER_FAVICON = '<link rel="icon" type="image/png" href="/favicon.ico">'
HOME_FAVICON = "<link rel=icon type=image/png href=/favicon.ico>"


def rule(selector):
    return re.search(r"(?:^|})" + re.escape(selector) + r"\{([^}]*)\}", home_style()).group(1)


def home_style():
    page = server.HOME_PAGE.read_text(encoding="utf-8")
    return re.search(r"<style>__HOME_PALETTE__(.*?)</style>", page, re.S).group(1).replace("\n", "")


@pytest.fixture
def rows():
    with (
        patch.object(server, "ledger_row", side_effect=lambda s, cells, control: f'<li class="row">{s["slug"]}</li>'),
        patch.object(server, "home_row", side_effect=lambda s, state, now: f'<li class="row">{s["slug"]}</li>'),
        patch.object(server, "home_cells", return_value=""),
        patch.object(server, "bin_cells", return_value=""),
        patch.object(server, "swarm_state", return_value=None),
        patch.object(server.ledger_bin, "entries", return_value=[]),
    ):
        yield


def summaries(*slugs):
    return [{"slug": slug, "closed_at": 0} for slug in slugs]


def test_home_reads_logo_agentihooks_home_without_a_ledger_count(rows):
    with patch.object(server, "ledger_summaries", return_value=summaries("a", "b")):
        page = server.index_page(now=0)
    assert f'{WATERMARK}<main class="home">{BRAND}<h1>HOME</h1>{server.FOLD_ALL}</header>' in page
    assert 'class="total"' not in page
    divider = rule("h1::before")
    assert "width:2px" in divider and "background:var(--signal)" in divider


@pytest.mark.parametrize(("slugs", "total"), [(("a",), "1 ledger"), (("a", "b"), "2 ledgers")])
def test_the_bin_keeps_its_count_and_has_no_watermark(rows, slugs, total):
    with patch.object(server, "bin_summaries", return_value=summaries(*slugs)):
        page = server.index_page(view="bin", now=0)
    assert f'<main class="bin">{BRAND}<h1>BIN</h1><span class="total">{total}</span></header>' in page
    assert f'</style><main class="bin">{BRAND}' in page


def test_the_watermark_is_faint_centred_half_the_viewport_and_never_takes_clicks():
    mark = rule(".watermark")
    for part in ("position:fixed", "top:50%", "left:50%", "translate(-50%,-50%)", "width:50vmin", "height:50vmin"):
        assert part in mark
    assert "pointer-events:none" in mark
    assert float(re.search(r"opacity:([.\d]+)", mark).group(1)) <= 0.08
    assert "position:relative" in rule("main") and "z-index:1" in rule("main")


def logo_links(page):
    return re.findall(r'<a [^>]*href="/"[^>]*>\s*<span class="logo" aria-hidden="true"></span>\s*</a>', page)


def test_the_logo_links_home_on_a_rendered_ledger_page_and_the_bin(rows):
    ledger = server.new_ledger.render(server.new_ledger.build_doc({"title": "Logo", "phases": []}), "logo", 8765)
    with patch.object(server, "bin_summaries", return_value=summaries("a")):
        bin_view = server.index_page(view="bin", now=0)
    with patch.object(server, "ledger_summaries", return_value=summaries("a")):
        home = server.index_page(now=0)
    for page in (ledger, bin_view, home):
        assert logo_links(page) == [LOGO_LINK]
        assert page.count('class="logo"') == 1


def test_the_logo_link_keeps_the_logo_box_and_marks_focus_from_the_palette():
    for css in (home_style(), core.TEMPLATE.read_text(encoding="utf-8")):
        link = re.search(r"\.logo-link ?\{([^}]*)\}", css).group(1).replace(" ", "")
        assert "display:flex" in link and "flex:none" in link
        focus = re.search(r"\.logo-link:focus-visible ?\{([^}]*)\}", css).group(1)
        assert "var(--" in focus and "#" not in focus


def test_the_logo_is_a_palette_coloured_mask_of_the_served_png():
    for selector in (".logo", ".watermark"):
        assert "background:var(--logo)" in rule(selector)
        assert "mask:url(/logo.png) center/contain no-repeat" in rule(selector)
    assert "--logo: var(" in core.PALETTE.read_text(encoding="utf-8")


def test_ledger_pages_show_the_logo_left_of_the_title_and_no_watermark():
    template = core.TEMPLATE.read_text(encoding="utf-8")
    masthead = f'<div class="masthead">{LOGO_LINK}<div>\n      <div class="title-row"'
    assert masthead in template
    assert "watermark" not in template
    assert "agentihooks</span>" not in template and ">HOME<" not in template
    css = re.search(r"\.logo \{([^}]*)\}", template).group(1)
    assert "background: var(--logo)" in css and "mask: url(/logo.png) center/contain no-repeat" in css


def test_the_server_serves_the_repo_logo_png(tmp_path):
    assert server.LOGO == server.ROOT / "media" / "agentihooks-logo.png"
    logo = tmp_path / "logo.png"
    logo.write_bytes(b"\x89PNG\r\n\x1a\nlogo")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, args=(0.01,), daemon=True).start()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{httpd.server_address[1]}/logo.png", headers={"Host": f"127.0.0.1:{server.PORT}"}
        )
        with patch.object(server, "LOGO", logo), urllib.request.urlopen(req) as resp:
            assert resp.status == 200
            assert resp.headers["Content-Type"] == "image/png"
            assert resp.headers["Content-Length"] == str(len(logo.read_bytes()))
            assert resp.headers["Cache-Control"] == "max-age=86400"
            assert resp.read() == logo.read_bytes()
        with patch.object(server, "LOGO", tmp_path / "missing.png"):
            with pytest.raises(urllib.error.HTTPError) as missing:
                urllib.request.urlopen(req)
        assert missing.value.code == 404
        assert missing.value.read() == b"no logo"
    finally:
        httpd.shutdown()
        httpd.server_close()


def logo_response(path):
    handler = server.Handler.__new__(server.Handler)
    handler.path, handler.headers, handler.wfile, sent = "/logo.png", {}, io.BytesIO(), []
    handler.send_response = sent.append
    handler.send_header = lambda name, value: sent.append((name, value))
    handler.end_headers = lambda: sent.append("end")
    with patch.object(server, "LOGO", path):
        handler.send_logo()
    return sent, handler.wfile.getvalue()


def test_send_logo_answers_the_png_with_a_day_of_caching_or_a_plain_404(tmp_path):
    logo = tmp_path / "logo.png"
    logo.write_bytes(b"\x89PNG\r\n\x1a\nlogo")
    sent, body = logo_response(logo)
    assert sent == [
        200,
        ("Content-Type", "image/png"),
        ("Content-Length", "12"),
        ("Cache-Control", "max-age=86400"),
        "end",
    ]
    assert body == logo.read_bytes()
    sent, body = logo_response(tmp_path / "missing.png")
    assert sent[:2] == [404, ("Content-Type", "text/plain")]
    assert body == b"no logo"


def test_the_server_serves_the_logo_as_the_favicon(tmp_path):
    logo = tmp_path / "logo.png"
    logo.write_bytes(b"\x89PNG\r\n\x1a\nlogo")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, args=(0.01,), daemon=True).start()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{httpd.server_address[1]}/favicon.ico", headers={"Host": f"127.0.0.1:{server.PORT}"}
        )
        with patch.object(server, "LOGO", logo), urllib.request.urlopen(req) as resp:
            assert resp.status == 200
            assert resp.headers["Content-Type"] == "image/png"
            assert resp.read() == logo.read_bytes()
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_every_page_shell_links_the_favicon_in_its_head(rows):
    ledger = server.new_ledger.render(server.new_ledger.build_doc({"title": "Icon", "phases": []}), "icon", 8765)
    with patch.object(server, "bin_summaries", return_value=summaries("a")):
        bin_view = server.index_page(view="bin", now=0)
    with patch.object(server, "ledger_summaries", return_value=summaries("a")):
        home = server.index_page(now=0)
    for page, link in ((ledger, LEDGER_FAVICON), (bin_view, HOME_FAVICON), (home, HOME_FAVICON)):
        assert page.count("favicon") == 1
        assert page.index("</title>") < page.index(link) < page.index("<style>")
