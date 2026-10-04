"""The ledger title: only the operator renames a ledger from its page; the slug never changes."""

OPS = ("title_set",)
MAX_TITLE = 200


def check(op):
    if set(op) != {"op", "id", "text"}:
        raise ValueError("title_set is the operator's and takes only id and text")
    if not isinstance(op["text"], str) or not op["text"].strip() or len(op["text"].strip()) > MAX_TITLE:
        raise ValueError(f"title_set needs a title of 1 to {MAX_TITLE} characters")


def apply(doc, op, ctx):
    text = op["text"].strip()
    if text != doc.get("title"):
        doc["title"] = text
        ctx.stamp("title", "operator")
        ctx.record("operator", "title changed", "title", id=op["id"], text=text)
    return True
