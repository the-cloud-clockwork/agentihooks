import argparse
import json
import math
import os
import signal
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from hooks.memory import archive, embeddings
from hooks.memory.api.ingest import Grant
from hooks.memory.catalog import Catalog
from hooks.memory.embeddings import EmbeddingModel, TransientEmbeddingError
from hooks.memory.index_worker import IndexWorker, Limits, Target

PREFIX = "AGENTIHOOKS_SESSION_"
REQUIRED = (
    "CATALOG",
    "EMBED_CORPUS",
    "EMBED_PROJECTS",
    "EMBED_GENERATION",
    "EMBED_MODEL",
    "EMBED_DIMENSION",
    "EMBED_URL",
    "EMBED_TOKEN",
)
ACTIVE_SESSIONS = (
    "SELECT c.session_uid, c.chunker_version FROM chunk_cutover c JOIN session_record r "
    "ON r.access_domain = c.access_domain AND r.session_uid = c.session_uid "
    "WHERE c.access_domain = ? AND r.state = 'retained' AND c.session_uid > ? ORDER BY c.session_uid LIMIT ?"
)
QUEUED_SESSIONS = "SELECT DISTINCT session_uid FROM embedding_work WHERE access_domain = ? AND index_generation = ?"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_):
        return None


@dataclass(frozen=True)
class Provider:
    model: EmbeddingModel
    url: str
    token: str = field(repr=False)

    def embed(self, texts: list[str]) -> list[list[float]]:
        payload = json.dumps({"model": self.model.model, "input": texts}).encode()
        request = urllib.request.Request(
            self.url,
            data=payload,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.build_opener(NoRedirect()).open(request, timeout=30) as response:
                data = response.read((2 << 20) + 1)
        except urllib.error.HTTPError as error:
            if error.code != 429 and error.code < 500:
                raise ValueError("embedding provider refused the request") from None
            raise TransientEmbeddingError("embedding provider unavailable") from None
        except (urllib.error.URLError, TimeoutError):
            raise TransientEmbeddingError("embedding provider unavailable") from None
        if len(data) > 2 << 20:
            raise ValueError("embedding response exceeds 2 MiB")
        body = json.loads(data)
        if body["model"] != self.model.model:
            raise ValueError("embedding provider returned another model")
        rows = sorted(body["data"], key=lambda item: item["index"])
        if [item["index"] for item in rows] != list(range(len(texts))):
            raise ValueError("embedding provider returned invalid indices")
        return embeddings.checked(self.model, texts, [item["embedding"] for item in rows])


@dataclass(frozen=True)
class Settings:
    catalog: str
    grant: Grant
    generation: str
    provider: Provider
    limits: Limits
    interval: float
    session_limit: int


def configure(environ: dict) -> Settings:
    if not all(environ.get(PREFIX + key) for key in REQUIRED):
        raise SystemExit("session embedding configuration is required: " + ", ".join(REQUIRED))
    projects = json.loads(environ[PREFIX + "EMBED_PROJECTS"])
    if (
        not isinstance(projects, list)
        or not projects
        or any(not isinstance(item, str) or not item for item in projects)
    ):
        raise ValueError("embedding projects must be a nonempty JSON array of project names")
    url = environ[PREFIX + "EMBED_URL"]
    parsed = urllib.parse.urlsplit(url)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("embedding URL must not carry credentials, query parameters or a fragment")
    if not parsed.hostname or (
        parsed.scheme != "https"
        and not (parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost", "::1"))
    ):
        raise ValueError("embedding URL requires HTTPS or loopback HTTP")
    model = embeddings.require(
        EmbeddingModel(environ[PREFIX + "EMBED_MODEL"], int(environ[PREFIX + "EMBED_DIMENSION"]))
    )
    limits = Limits(
        *(
            int(environ.get(PREFIX + key, default))
            for key, default in (("EMBED_BATCH_SIZE", "32"), ("EMBED_BATCH_CHARS", "64000"), ("EMBED_MAX_BATCHES", "8"))
        )
    )
    interval = float(environ.get(PREFIX + "EMBED_INTERVAL", "5"))
    session_limit = int(environ.get(PREFIX + "EMBED_SESSION_LIMIT", "100"))
    if (
        min(limits.batch_size, limits.batch_chars, limits.max_batches, session_limit) < 1
        or not math.isfinite(interval)
        or interval <= 0
    ):
        raise ValueError("embedding bounds and interval must be positive and finite")
    grant = Grant("session-embedding-process", environ[PREFIX + "EMBED_CORPUS"], frozenset(projects))
    return Settings(
        environ[PREFIX + "CATALOG"],
        grant,
        environ[PREFIX + "EMBED_GENERATION"],
        Provider(model, url, environ[PREFIX + "EMBED_TOKEN"]),
        limits,
        interval,
        session_limit,
    )


def cycle(worker: IndexWorker, settings: Settings, after: str) -> dict:
    grant, generation = settings.grant, settings.generation
    rows = worker.catalog.db.execute(ACTIVE_SESSIONS, (grant.corpus, after, settings.session_limit)).fetchall()
    skipped = 0
    for uid, version in rows:
        try:
            worker.enqueue(grant, Target(grant.corpus, uid, version, generation), time.time())
        except archive.EvidenceRefused:
            skipped += 1
    pending = worker.catalog.db.execute(QUEUED_SESSIONS, (grant.corpus, generation)).fetchall()
    for (uid,) in pending:
        archive._readable(worker.catalog, grant, grant.corpus, uid)
    report = worker.drain(settings.provider, grant.corpus, generation, time.time())
    return {**report, "skipped": skipped, "after": rows[-1][0] if len(rows) == settings.session_limit else ""}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    settings = configure(dict(os.environ))
    stopped = threading.Event()
    previous = {sig: signal.signal(sig, lambda *_: stopped.set()) for sig in (signal.SIGTERM, signal.SIGINT)}
    catalog = Catalog(settings.catalog)
    try:
        worker = IndexWorker(catalog, settings.limits)
        worker.register(settings.grant.corpus, settings.generation, settings.provider.model)
        after = ""
        while not stopped.is_set():
            report = cycle(worker, settings, after)
            print(json.dumps(report), flush=True)
            after = report["after"]
            if args.once:
                break
            stopped.wait(settings.interval)
    finally:
        catalog.close()
        for sig, prior in previous.items():
            signal.signal(sig, prior)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
