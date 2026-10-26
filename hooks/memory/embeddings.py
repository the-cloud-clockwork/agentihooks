"""Embedding model identity and the provider contract the index worker calls."""

from dataclasses import dataclass
from typing import Protocol

from hooks.memory.catalog import Conflict

BAD_MODEL = "an embedding model needs an identity and a positive dimension"
DIMENSION_MISMATCH = (
    "the provider returned vectors whose count or dimension differs from the model; nothing was written"
)


@dataclass(frozen=True)
class EmbeddingModel:
    model: str
    dimension: int


class TransientEmbeddingError(Exception):
    pass


class Embedder(Protocol):
    """An adapter raises TransientEmbeddingError for a provider outage; any other exception is a defect."""

    model: EmbeddingModel

    def embed(self, texts: list[str]) -> list[list[float]]: ...


def require(model: EmbeddingModel) -> EmbeddingModel:
    if not model.model or model.dimension < 1:
        raise ValueError(BAD_MODEL)
    return model


def checked(model: EmbeddingModel, texts: list[str], vectors: list[list[float]]) -> list[list[float]]:
    if len(vectors) != len(texts) or any(len(vector) != model.dimension for vector in vectors):
        raise Conflict(DIMENSION_MISMATCH)
    return [[float(value) for value in vector] for vector in vectors]
