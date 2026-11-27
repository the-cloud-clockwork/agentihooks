import json
from typing import Any

WAITING = "agentihooks-ci:waiting"
UNANSWERED_FETCHES = 10


class FetchUnanswered(ValueError):
    pass


class HoldsForever:
    def _release(self, name: str, held: dict[str, Any], now_ms: int) -> list[str]:
        self: Any
        try:
            return [self._start(held["request"])]
        except FetchUnanswered as error:
            # ruleid: lab-unanswered-fetch-holds-forever
            self.client.hset(WAITING, name, json.dumps({**held, "since": now_ms}))
            return [f"holding {held['request']}: {error}"]


class CountsWithNoCap:
    def _release(self, name: str, held: dict[str, Any], now_ms: int) -> list[str]:
        self: Any
        try:
            return [self._start(held["request"])]
        except FetchUnanswered as error:
            fetches = held.get("unanswered", 0) + 1
            # ruleid: lab-unanswered-fetch-holds-forever
            self.client.hset(WAITING, name, json.dumps({**held, "since": now_ms, "unanswered": fetches}))
            return [f"holding {held['request']}: {error}"]


class HoldsUpToTheCap:
    def _release(self, name: str, held: dict[str, Any], now_ms: int) -> list[str]:
        self: Any
        try:
            return [self._start(held["request"])]
        except FetchUnanswered as error:
            fetches = held.get("unanswered", 0) + 1
            if fetches < UNANSWERED_FETCHES:
                # ok: lab-unanswered-fetch-holds-forever
                self.client.hset(WAITING, name, json.dumps({**held, "since": now_ms, "unanswered": fetches}))
                return [f"holding {held['request']}: {error}"]
            return [f"cannot expand {held['request']}: {fetches} manifest fetches in a row got no answer: {error}"]


class KeepsTheWindowWhileFetching:
    def _release(self, name: str, raw: str) -> list[str]:
        self: Any
        # ok: lab-unanswered-fetch-holds-forever
        self.client.hset(WAITING, name, raw)
        return [f"holding {name}"]


class TakesTheGradeRef:
    def _hold(self, name: str, waiting: dict[str, Any], ref: str) -> str:
        self: Any
        # ok: lab-unanswered-fetch-holds-forever
        self.client.hset(WAITING, name, json.dumps({**waiting, "ref": ref}))
        return f"holding {waiting['request']}"
