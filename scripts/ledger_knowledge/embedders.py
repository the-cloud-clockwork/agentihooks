"""The knowledge index embedder setting: the hashed word embedder or a semantic embeddings provider."""

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass

from hooks.memory.embedding_process import Provider, checked_url
from hooks.memory.embeddings import EmbeddingModel, require

from .index import HASH, Embedder, HashEmbedder

PREFIX = "AGENTIHOOKS_KNOWLEDGE_"
SEMANTIC = "semantic"
REQUIRED = ("EMBED_URL", "EMBED_MODEL", "EMBED_DIMENSION", "EMBED_TOKEN")
BATCH = 32


@dataclass(frozen=True)
class Batched:
    """Sends texts to an inner embedder in batches of at most size texts."""

    inner: Embedder
    size: int

    def embed(self, texts: list[str]) -> list[list[float]]:
        """One vector per text in order, one inner call per batch."""
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.size):
            vectors.extend(self.inner.embed(texts[start : start + self.size]))
        return vectors


@dataclass(frozen=True)
class Choice:
    """The chosen embedder and the name its index file carries."""

    name: str
    embedder: Embedder


def _semantic(environ: Mapping[str, str]) -> Choice:
    if any(not environ.get(PREFIX + key) for key in REQUIRED):
        raise ValueError("semantic embedder needs " + ", ".join(PREFIX + key for key in REQUIRED))
    model = require(EmbeddingModel(environ[PREFIX + "EMBED_MODEL"], int(environ[PREFIX + "EMBED_DIMENSION"])))
    size = int(environ.get(PREFIX + "EMBED_BATCH", str(BATCH)))
    if size < 1:
        raise ValueError("semantic embedder batch must be positive")
    provider = Provider(model, checked_url(environ[PREFIX + "EMBED_URL"]), environ[PREFIX + "EMBED_TOKEN"])
    identity = hashlib.sha256(f"{model.model}\n{model.dimension}".encode()).hexdigest()[:12]
    return Choice(f"{SEMANTIC}-{identity}", Batched(provider, size))


def choose(environ: Mapping[str, str]) -> Choice:
    """The embedder AGENTIHOOKS_KNOWLEDGE_EMBEDDER names, the hashed word embedder by default."""
    kind = environ.get(PREFIX + "EMBEDDER", HASH)
    if kind == HASH:
        return Choice(HASH, HashEmbedder())
    if kind == SEMANTIC:
        return _semantic(environ)
    raise ValueError(f"{PREFIX}EMBEDDER must be {HASH} or {SEMANTIC}")
