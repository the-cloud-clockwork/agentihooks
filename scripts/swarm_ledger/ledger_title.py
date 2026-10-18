"""The ledger title and overview: only the operator edits them from the page; the slug never changes."""

OPS = ("title_set", "overview_set")
MAX_TITLE = 200
MAX_OVERVIEW = 20000


def check(op):
    if set(op) != {"op", "id", "text"}:
        raise ValueError(f"{op.get('op')} is the operator's and takes only id and text")
    if op["op"] == "overview_set":
        if not isinstance(op["text"], str) or len(op["text"].strip()) > MAX_OVERVIEW:
            raise ValueError(f"overview_set needs an overview of at most {MAX_OVERVIEW} characters")
        return
    if not isinstance(op["text"], str) or not op["text"].strip() or len(op["text"].strip()) > MAX_TITLE:
        raise ValueError(f"title_set needs a title of 1 to {MAX_TITLE} characters")


def apply(doc, op, ctx):
    field = "overview" if op["op"] == "overview_set" else "title"
    text = op["text"].strip()
    if text != doc.get(field):
        doc[field] = text
        ctx.stamp(field, "operator")
        ctx.record("operator", f"{field} changed", field, id=op["id"], text=text)
    return True
