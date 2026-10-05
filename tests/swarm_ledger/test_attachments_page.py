import json
import subprocess

from tests.swarm_ledger.test_outline import TEMPLATE, function_source

PNG_ID = "a" * 64 + ".png"
ATT = {"id": PNG_ID, "type": "image/png", "size": 10, "width": 30, "height": 20}
PRELUDE = (
    "const SLUG = 'shots'; const TOKEN = 'tok'; const API = '/api/shots'; const MAX_ATTACH = 6;"
    "const MEDIA_API = '/api/media/shots'; const attaching = {}; const attachNote = {}; const nodes = {};"
    "const $ = id => nodes[id] ||= {value: '', kids: [], replaceChildren(...k) { this.kids = k; }};"
    "const h = (tag, attrs, ...kids) => ({tag, ...attrs, kids: kids.filter(Boolean)});"
    "let sent = []; const queue = op => sent.push(op); const grow = () => {}; const newId = () => 'm-1';"
    "let refreshed = []; const refreshTray = key => refreshed.push(key);"
)


def source(name):
    page, head = TEMPLATE.read_text(encoding="utf-8"), f"  async function {name}("
    if head not in page:
        return function_source(name)
    return f"async function {name}(" + page.split(head, 1)[1].split("\n  }\n", 1)[0] + "\n}"


def run(names, body):
    script = PRELUDE + "\n".join(source(n) for n in names) + "\n" + body
    done = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
    return json.loads(done.stdout)


def test_a_chat_line_carries_its_attachments_and_clears_the_tray():
    result = run(
        ["chatText", "withAttachments", "sendChat"],
        f"attaching.chat = [{json.dumps(ATT)}]; sendChat('');"
        "console.log(JSON.stringify({sent, left: attaching.chat || null, refreshed}));",
    )
    assert result["sent"] == [{"op": "add", "thread": "chat", "id": "m-1", "text": "", "attachments": [ATT]}]
    assert result["left"] is None
    assert result["refreshed"] == ["chat"]


def test_a_line_without_attachments_stays_plain():
    result = run(["withAttachments"], "console.log(JSON.stringify(withAttachments({op: 'add', text: 'x'}, 'k')));")
    assert result == {"op": "add", "text": "x"}


def test_the_optimistic_entry_shows_its_attachments_before_the_server_answers():
    result = run(
        ["itemOf", "threadOf", "applyOp"],
        "const d = {chat: []};"
        f"applyOp(d, {{op: 'add', thread: 'chat', id: 'm-2', text: '', attachments: [{json.dumps(ATT)}]}});"
        "console.log(JSON.stringify(d.chat[0].attachments));",
    )
    assert result == [ATT]


def test_a_thumbnail_links_to_the_full_size_image_in_a_new_tab():
    result = run(
        ["mediaUrl", "thumb", "attachmentsView"], f"console.log(JSON.stringify(attachmentsView([{json.dumps(ATT)}])));"
    )
    link = result["kids"][0]
    assert link["tag"] == "a" and link["href"] == f"/media/shots/{PNG_ID}" and link["target"] == "_blank"
    img = link["kids"][0]
    assert (img["tag"], img["src"], img["width"], img["height"]) == ("img", f"/media/shots/{PNG_ID}", 30, 20)


def test_no_attachments_render_nothing():
    assert (
        run(["mediaUrl", "thumb", "attachmentsView"], "console.log(JSON.stringify(attachmentsView(undefined)));")
        is None
    )


def test_each_file_is_uploaded_with_the_token_and_a_refusal_is_shown_on_the_tray():
    result = run(
        ["addImages"],
        "const calls = []; const replies = ["
        f"{{ok: true, json: async () => ({json.dumps(ATT)})}},"
        "{ok: false, text: async () => 'only PNG, JPEG, WebP or GIF images are accepted'}];"
        "globalThis.fetch = async (url, init) => { calls.push([url, init.method, init.headers['X-Ledger-Token'], init.body]);"
        " return replies.shift(); };"
        "addImages('tasks/t1/comments', [{name: 'a.png'}, {name: 'b.txt'}]).then(() =>"
        " console.log(JSON.stringify({calls, list: attaching['tasks/t1/comments'], note: attachNote['tasks/t1/comments']})));",
    )
    assert result["calls"] == [
        ["/api/media/shots", "POST", "tok", {"name": "a.png"}],
        ["/api/media/shots", "POST", "tok", {"name": "b.txt"}],
    ]
    assert result["list"] == [ATT]
    assert "PNG, JPEG, WebP or GIF" in result["note"]


def test_the_preview_offers_a_remove_control_per_image():
    result = run(
        ["mediaUrl", "thumb", "trayView"],
        f"attaching.chat = [{json.dumps(ATT)}]; const tray = trayView('chat');"
        "tray.kids[0].kids[1].on.click();"
        "console.log(JSON.stringify({label: tray.kids[0].kids[1].text, left: attaching.chat, refreshed}));",
    )
    assert result == {"label": "Remove", "left": [], "refreshed": ["chat"]}


def test_chat_and_comment_composers_take_paste_drop_and_pick_and_notes_do_not():
    page = TEMPLATE.read_text(encoding="utf-8")
    attach = function_source("attachable")
    for needle in ('"paste"', '"drop"', '"dragover"', 'type: "file"', "accept: ACCEPT"):
        assert needle in attach, needle
    assert 'attachable("chat"' in page
    assert 'path.endsWith("/comments")' in function_source("threadView")
    assert 'const ACCEPT = "image/png,image/jpeg,image/webp,image/gif";' in page
