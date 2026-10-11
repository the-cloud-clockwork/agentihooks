"""Models each harness picker offers, read from the harness's own source and cached in Redis for one hour."""

import json
import re
import subprocess
import urllib.request

from scripts.account_names import OAUTH_ENV, oauth_env
from scripts.codex_quota import codex_home
from scripts.swarm.keyspace import ROOT

TTL = 3600
KEY = f"{ROOT}:swarm:models"
MODELS_URL = "https://api.anthropic.com/v1/models?limit=1000"
AUTHORIZATION = "Authorization"
HEADERS = {"anthropic-version": "2023-06-01", "anthropic-beta": "oauth-2025-04-20"}
HELP_MODEL = re.compile(r"--model <model>(.*?)\n  --", re.S)
HELP_EFFORT = re.compile(r"--effort <level>(.*?)\n  --", re.S)
LEVELS = re.compile(r"\(([a-z, ]+)\)")
ALIAS = re.compile(r"'([a-z]+)'")


def entry(model, label, efforts):
    return {"model": model, "label": label, "efforts": list(efforts)}


def codex(environ):
    try:
        models = json.loads((codex_home(environ) / "models_cache.json").read_bytes())["models"]
        listed = sorted((m for m in models if m["visibility"] == "list"), key=lambda m: m["priority"])
        return [
            entry(
                m["slug"],
                m.get("display_name") or m["slug"],
                [level["effort"] for level in m["supported_reasoning_levels"]],
            )
            for m in listed
        ]
    except (OSError, ValueError, KeyError, TypeError):
        return []


def fetch_models(token):
    request = urllib.request.Request(MODELS_URL, headers={AUTHORIZATION: f"Bearer {token}", **HEADERS})
    with urllib.request.urlopen(request, timeout=10) as reply:
        return json.load(reply)["data"]


def from_api(models):
    newest = {}
    for model in models:
        line = model["line"]
        if line not in newest or model["created_at"] > newest[line]["created_at"]:
            newest[line] = model
    return [
        entry(
            line,
            model["display_name"],
            [
                name
                for name, flag in model["capabilities"]["effort"].items()
                if isinstance(flag, dict) and flag["supported"]
            ],
        )
        for line, model in newest.items()
    ]


def cli_help():
    return subprocess.run(["claude", "--help"], capture_output=True, text=True, timeout=20, check=False).stdout


def from_help(text):
    model = HELP_MODEL.search(text)
    effort = HELP_EFFORT.search(text)
    levels = LEVELS.search(effort.group(1)) if effort else None
    efforts = [name.strip() for name in levels.group(1).split(",")] if levels else []
    return [entry(alias, alias, efforts) for alias in ALIAS.findall(model.group(1))] if model else []


def from_account(environ):
    token = oauth_env(environ).get(OAUTH_ENV) or environ.get(OAUTH_ENV)
    try:
        return from_api(fetch_models(token)) if token else []
    except (OSError, ValueError, KeyError, TypeError):
        return []


def documented():
    try:
        return from_help(cli_help())
    except (OSError, subprocess.SubprocessError):
        return []


def claude(environ):
    return from_account(environ) or documented()


READERS = {"claude": claude, "codex": codex}


def cached(harness, environ, redis, now):
    key = f"{KEY}:{harness}"
    held = json.loads(redis.get(key) or "null") if redis is not None else None
    if held and now - held["at"] < TTL:
        return held["models"]
    models = READERS[harness](environ)
    if models and redis is not None:
        redis.set(key, json.dumps({"at": now, "models": models}), ex=TTL)
    return models


def catalog(environ, redis, now):
    return {harness: cached(harness, environ, redis, now) for harness in READERS}
