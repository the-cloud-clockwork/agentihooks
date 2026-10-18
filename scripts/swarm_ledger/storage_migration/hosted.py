import argparse
import base64
import fcntl
import gzip
import hashlib
import hmac
import io
import json
import os
import re
import time
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from scripts.swarm_ledger.api.errors import APIError
from scripts.swarm_ledger.repository.sqlite import DATABASE, SQLiteLedgerRepository, read_only

LIFETIME = 900
MAX_COMPRESSED = 64 << 20
MAX_EXPANDED = 256 << 20
FILE_ID = re.compile(r"[0-9a-f]{64}\.[a-z0-9]+\Z")
TABLE_ID = re.compile(r"[a-z_]+\Z")
FENCES = "CREATE TABLE IF NOT EXISTS hosted_fences (slug TEXT PRIMARY KEY, destination TEXT NOT NULL)"
FENCED = "SELECT destination FROM hosted_fences WHERE slug IN (?, '*')"
FENCE_DESTINATIONS = "SELECT destination FROM hosted_fences WHERE slug IN (?, '*') OR ?='*'"
CATALOG = "SELECT slug FROM ledgers ORDER BY slug"
REGISTRIES = "SELECT slug,path,value FROM registry WHERE path=? ORDER BY slug,path"
TOKEN_MESSAGE = "ledger-import:{slug}:{expires}"


def encode(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def token(secret: str, slug: str, expires: int) -> str:
    message = TOKEN_MESSAGE.format(slug=slug, expires=expires)
    return f"{expires}.{hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()}"


def authorize(secret: str, slug: str, credential: str, now: int) -> bool:
    try:
        expires = int(credential.split(".")[0])
    except (ValueError, AttributeError):
        return False
    return (
        bool(secret)
        and now < expires <= now + LIFETIME
        and hmac.compare_digest(credential.encode(), token(secret, slug, expires).encode())
    )


@contextmanager
def media_lock(directory: Path) -> Iterator[None]:
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".ledger-media.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


@contextmanager
def media_mutation(directory: Path, slug: str) -> Iterator[bool]:
    with media_lock(directory), read_only(directory) as connection:
        exists = (
            connection is not None
            and connection.execute("SELECT 1 FROM sqlite_master WHERE name='hosted_fences'").fetchone()
        )
        yield not (exists and connection.execute(FENCED, (slug,)).fetchone())


def fence(repo: SQLiteLedgerRepository, slug: str, destination: str) -> None:
    with repo.domain.LOCK, repo.connect() as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(FENCES)
        previous = connection.execute(FENCE_DESTINATIONS, (slug, slug)).fetchall()
        if any(row[0] != destination for row in previous):
            raise ValueError("ledger already has a different hosted destination")
        if slug != "*" and not connection.execute("SELECT 1 FROM ledgers WHERE slug=?", (slug,)).fetchone():
            raise ValueError("no such ledger")
        tables = connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        for (table,) in tables:
            if table == "hosted_fences" or not TABLE_ID.fullmatch(table):
                continue
            columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
            if "slug" not in columns:
                continue
            column = "path" if table == "registry" else "slug"
            for action, reference in (("INSERT", "NEW"), ("UPDATE", "OLD"), ("DELETE", "OLD")):
                connection.execute(
                    f"CREATE TRIGGER IF NOT EXISTS hosted_{table}_{action} BEFORE {action} ON {table} "
                    f"WHEN EXISTS(SELECT 1 FROM hosted_fences WHERE slug IN ({reference}.{column}, '*')) "
                    "BEGIN SELECT RAISE(ABORT,'ledger moved to hosted service'); END"
                )
        with media_lock(repo.directory):
            connection.execute("INSERT OR IGNORE INTO hosted_fences VALUES (?,?)", (slug, destination))
            connection.commit()


def capture(repo: SQLiteLedgerRepository, slug: str) -> dict:
    with read_only(repo.directory) as connection:
        if connection is None:
            raise ValueError("no ledger database")
        connection.execute("BEGIN")
        state = repo._export(connection, slug)
        registries = [list(row) for row in connection.execute(REGISTRIES, (slug,))]
        folder = repo.directory / f"{slug}.media"
        files = {}
        if folder.is_dir():
            for path in sorted(folder.iterdir()):
                if path.is_symlink() or not path.is_file():
                    raise ValueError("ledger media must contain regular files")
                files[path.name] = base64.b64encode(path.read_bytes()).decode()
        return {"version": 1, "slug": slug, "state": state, "registries": registries, "files": files}


def manifest(snapshot: dict) -> dict:
    state = snapshot["state"]
    meta = state["_meta"]
    return {
        "slug": snapshot["slug"],
        "revision": meta["rev"],
        "tasks": len(state.get("tasks", [])),
        "events": len(meta.get("events", [])),
        "revisions": len(meta.get("seeds", {})),
        "artifacts": len(state.get("artifacts", [])),
        "artifact_trash": len(state.get("artifact_trash", [])),
        "files": len(snapshot["files"]),
        "registries": len(snapshot["registries"]),
        "digest": hashlib.sha256(encode(snapshot)).hexdigest(),
    }


def file_bytes(files: dict) -> dict:
    result = {}
    for name, value in files.items():
        if not FILE_ID.fullmatch(name):
            raise ValueError("invalid content addressed file name")
        data = base64.b64decode(value, validate=True)
        if hashlib.sha256(data).hexdigest() != name.split(".")[0]:
            raise ValueError("file content does not match its name")
        result[name] = data
    return result


def write_files(folder: Path, files: dict) -> None:
    folder.mkdir(exist_ok=True)
    for name, data in files.items():
        path = folder / name
        if path.is_symlink() or (path.exists() and path.read_bytes() != data):
            raise ValueError("hosted file conflict")
        if not path.exists():
            temporary = path.with_suffix(path.suffix + ".part")
            temporary.write_bytes(data)
            temporary.replace(path)


def restore(repo: SQLiteLedgerRepository, snapshot: dict) -> dict:
    slug, state = snapshot["slug"], snapshot["state"]
    if snapshot["version"] != 1 or not repo.domain.SLUG_RE.fullmatch(slug):
        raise ValueError("invalid migration identity")
    files = file_bytes(snapshot["files"])
    if any(path != slug for _, path, _ in snapshot["registries"]):
        raise ValueError("invalid migration registry")
    with repo.domain.LOCK, repo.connect() as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        exists = connection.execute("SELECT 1 FROM ledgers WHERE slug=?", (slug,)).fetchone()
        if exists:
            if capture(repo, slug) != snapshot:
                raise ValueError("hosted ledger conflict")
            return manifest(snapshot)
        write_files(repo.directory / f"{slug}.media", files)
        stored = json.loads(encode(state))
        seeds = stored["_meta"].get("seeds")
        if seeds is not None:
            stored["_meta"]["seeds"] = {}
        import secrets

        repo._insert(connection, slug, stored, secrets.token_urlsafe(), seeds)
        for registry, path, value in snapshot["registries"]:
            connection.execute("INSERT INTO registry VALUES (?,?,?)", (registry, path, value))
        if repo._export(connection, slug) != state:
            raise ValueError("imported history differs from source")
    return manifest(capture(repo, slug))


def route(handler: object, server: object, parts: list) -> dict:
    if len(parts) != 2 or not server.core.SLUG_RE.fullmatch(parts[1]):
        raise APIError(404, "resource_missing", "No such migration")
    slug = parts[1]
    if not authorize(
        os.environ.get("SWARM_SECRET", ""), slug, handler.headers.get("X-Ledger-Migration", ""), int(time.time())
    ):
        raise APIError(403, "forbidden", "Missing or expired migration credential")
    if handler.command == "GET":
        return {"data": manifest(capture(server.repository, slug))}
    if handler.command != "POST":
        raise APIError(405, "method_not_allowed", "Use GET or POST for migration")
    if handler.headers.get("Content-Type") != "application/gzip":
        raise APIError(415, "content_type", "Migration requires application/gzip")
    try:
        length = int(handler.headers.get("Content-Length", "0"))
        if not 0 < length <= MAX_COMPRESSED:
            raise ValueError("migration body exceeds compressed limit")
        with gzip.GzipFile(fileobj=io.BytesIO(handler.rfile.read(length))) as stream:
            data = stream.read(MAX_EXPANDED + 1)
        if len(data) > MAX_EXPANDED:
            raise ValueError("migration body exceeds expanded limit")
        snapshot = json.loads(data)
        if snapshot["slug"] != slug:
            raise ValueError("migration slug differs from route")
        result = restore(server.repository, snapshot)
    except (ValueError, KeyError, TypeError, EOFError, OSError) as error:
        raise APIError(400, "migration_invalid", "Migration refused: invalid data or existing conflict") from error
    return {"data": result}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def transfer(url: str, snapshot: dict, secret: str) -> dict:
    headers = {
        "User-Agent": "agentihooks-swarm",
        "Content-Type": "application/gzip",
        "X-Ledger-Migration": token(secret, snapshot["slug"], int(time.time()) + LIFETIME),
        "CF-Access-Client-Id": os.environ.get("SWARM_ACCESS_CLIENT_ID") or os.environ.get("CF_ACCESS_CLIENT_ID", ""),
        "CF-Access-Client-Secret": os.environ.get("SWARM_ACCESS_CLIENT_SECRET")
        or os.environ.get("CF_ACCESS_CLIENT_SECRET", ""),
    }
    from scripts.swarm_ledger.ledger_link import guard

    guard(url, headers)
    endpoint = url.rstrip("/") + "/api/v1/migrations/" + snapshot["slug"]
    opener = urllib.request.build_opener(NoRedirect)
    request = urllib.request.Request(endpoint, data=gzip.compress(encode(snapshot)), headers=headers)
    with opener.open(request, timeout=300) as response:
        result = json.load(response)["data"]
    with opener.open(urllib.request.Request(endpoint, headers=headers), timeout=300) as response:
        verified = json.load(response)["data"]
    if result != verified or verified != manifest(snapshot):
        raise ValueError("hosted comparison differs; do not cut over")
    return {"source": manifest(snapshot), "hosted": verified, "match": True}


def server_secret() -> str:
    if secret := os.environ.get("SWARM_SECRET"):
        return secret
    base = os.environ.get("OPENBAO_URL", "").rstrip("/")
    credential = os.environ.get("OPENBAO_TOKEN")
    if not base or not credential:
        raise ValueError("trusted OpenBao credentials unavailable")
    request = urllib.request.Request(base + "/v1/secret/data/k8s/swarm", headers={"X-Vault-Token": credential})
    with urllib.request.build_opener(NoRedirect).open(request, timeout=30) as response:
        secret = json.load(response)["data"]["data"].get("SWARM_SECRET")
    if not secret:
        raise ValueError("OpenBao record k8s/swarm has no server secret")
    return secret


def main() -> None:
    from hooks import config  # noqa: F401

    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--url")
    parser.add_argument("--slug", action="append")
    parser.add_argument("--freeze", action="store_true")
    args = parser.parse_args()
    if args.url and not args.url.startswith("https://"):
        parser.error("hosted destination must use HTTPS")
    if args.freeze and not args.url:
        parser.error("freezing requires a hosted destination")
    if args.url and not args.freeze:
        parser.error("hosted transfers require source fencing with --freeze")
    secret = server_secret() if args.url else None
    from scripts.swarm_ledger import ledger_server

    repo = SQLiteLedgerRepository(args.source / DATABASE, ledger_server.core)
    with read_only(args.source) as connection:
        slugs = args.slug or [row[0] for row in connection.execute(CATALOG)]
        if args.freeze and not args.slug:
            fence(repo, "*", args.url)
            slugs = [row[0] for row in connection.execute(CATALOG)]
    for slug in slugs:
        if args.freeze:
            fence(repo, slug, args.url)
        snapshot = capture(repo, slug)
        result = transfer(args.url, snapshot, secret) if args.url else manifest(snapshot)
        print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
