"""The page layout: panel sizes and column widths shared by every ledger page, HOME and BIN."""

import json
import re

import ledger_core as core

ROWS = {
    "capacity-box": ("height",),
    "quota-box": ("height",),
    "overlays-box": ("height",),
    "agents-box": ("height",),
    "health-box": ("height",),
    "handoff-box": ("height",),
}
LIMITS = {"height": (48, 4000, 0), "split": (15, 85, 1)}
COLUMNS = "columns"
TABLE = re.compile(r"^[a-z][a-z0-9-]{0,48}$")
COLUMN_WIDTH = (0, 2000)
MAX_TABLES = 32
MAX_COLUMNS = 24


def path():
    return core.LEDGER_DIR / ".swarm-layout"


def size(row, key, value):
    low, high, digits = LIMITS[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
        raise ValueError(f"{row} {key} must be a number from {low} to {high}")
    return round(value, digits) if digits else round(value)


def columns(tables):
    if not isinstance(tables, dict) or len(tables) > MAX_TABLES:
        raise ValueError(f"columns must be an object of at most {MAX_TABLES} tables")
    low, high = COLUMN_WIDTH
    found = {}
    for table, widths in tables.items():
        if not TABLE.match(table):
            raise ValueError(f"unknown column table {table}")
        if not isinstance(widths, list) or not 0 < len(widths) <= MAX_COLUMNS:
            raise ValueError(f"{table} needs 1 to {MAX_COLUMNS} column widths")
        if any(isinstance(w, bool) or not isinstance(w, (int, float)) or not low <= w <= high for w in widths):
            raise ValueError(f"{table} column widths must be numbers from {low} to {high}")
        found[table] = [round(w) for w in widths]
    return found


def check(body):
    if not isinstance(body, dict):
        raise ValueError("layout must be an object of rows")
    layout = {}
    for row, sizes in body.items():
        if row == COLUMNS:
            layout[COLUMNS] = columns(sizes)
            continue
        if row not in ROWS:
            raise ValueError(f"unknown row {row}")
        if not isinstance(sizes, dict) or not sizes:
            raise ValueError(f"{row} must be an object of sizes")
        if set(sizes) - set(ROWS[row]):
            raise ValueError(f"{row} takes only {', '.join(ROWS[row])}")
        layout[row] = {key: size(row, key, value) for key, value in sizes.items()}
    return layout


def read():
    try:
        saved = core.loads(path().read_bytes())
        return check({row: sizes for row, sizes in saved.items() if row in ROWS or row == COLUMNS})
    except (OSError, ValueError):
        return {}


def loads(data):
    try:
        return core.loads(data or b"{}")
    except json.JSONDecodeError:
        raise ValueError("body is not JSON") from None


def write(body):
    layout = check(body)
    core.atomic_write(path(), json.dumps(layout))
    return layout
