from collections.abc import Mapping
from typing import Protocol


class Settings:
    def __init__(self, trusted: frozenset[str], served: frozenset[str] | None = None) -> None:
        self.trusted, self.served = trusted, served


class Build(Protocol):
    def build(self, environ: Mapping[str, str]) -> Settings: ...


class EveryRepository(Build):
    def build(self, environ: Mapping[str, str]) -> Settings:
        # ruleid: lab-scheduler-built-without-served-repositories
        return Settings(frozenset({environ["CI_LAB_REPOSITORY"]}))


class NoRepositoryNamed(Build):
    def build(self, environ: Mapping[str, str]) -> Settings:
        # ruleid: lab-scheduler-built-without-served-repositories
        return Settings(frozenset({environ["CI_LAB_REPOSITORY"]}), served=None)


class ServedRepositories(Build):
    def build(self, environ: Mapping[str, str]) -> Settings:
        repository = environ["CI_LAB_REPOSITORY"]
        # ok: lab-scheduler-built-without-served-repositories
        return Settings(frozenset({repository}), served=frozenset({repository}))


def elsewhere() -> Settings:
    # ok: lab-scheduler-built-without-served-repositories
    return Settings(frozenset())
