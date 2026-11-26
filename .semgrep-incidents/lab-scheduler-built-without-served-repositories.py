from collections.abc import Mapping


class Settings:
    def __init__(self, trusted: frozenset[str], served: frozenset[str] | None = None) -> None:
        self.trusted, self.served = trusted, served


class EveryRepository:
    def build(self, environ: Mapping[str, str]) -> Settings:
        # ruleid: lab-scheduler-built-without-served-repositories
        return Settings(frozenset({environ["CI_LAB_REPOSITORY"]}))


class NoRepositoryNamed:
    def build(self, environ: Mapping[str, str]) -> Settings:
        # ruleid: lab-scheduler-built-without-served-repositories
        return Settings(frozenset({environ["CI_LAB_REPOSITORY"]}), served=None)


class ServedRepositories:
    def build(self, environ: Mapping[str, str]) -> Settings:
        repository = environ["CI_LAB_REPOSITORY"]
        # ok: lab-scheduler-built-without-served-repositories
        return Settings(frozenset({repository}), served=frozenset({repository}))


class Elsewhere:
    def settings(self) -> Settings:
        # ok: lab-scheduler-built-without-served-repositories
        return Settings(frozenset())
