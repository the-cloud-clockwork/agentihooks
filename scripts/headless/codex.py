import json
import subprocess
from collections.abc import Mapping
from pathlib import Path

from scripts import codex_router
from scripts.headless.request import KEPT, SEALED, Request, execute

CODEX = "codex"
SEALED_FEATURES = (
    "shell_tool",
    "unified_exec",
    "code_mode",
    "code_mode_host",
    "apps",
    "plugins",
    "multi_agent",
    "multi_agent_v2",
    "goals",
    "sleep_tool",
    "view_image",
    "image_generation",
    "browser_use",
    "computer_use",
    "skill_search",
    "tool_suggest",
    "hooks",
)
READ_ONLY_FEATURES = ("apps", "plugins", "multi_agent", "hooks")
CATALOG_DROPPED = ("tool_mode", "apply_patch_tool_type", "web_search_tool_type", "multi_agent_version")


def toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")


def arguments(request: Request, work: Path) -> list[str]:
    sealed = request.clamp == SEALED
    args = ["exec", "--ignore-user-config", "--ignore-rules", "--ephemeral", "--skip-git-repo-check"]
    args += ["--sandbox", "read-only", "--output-schema", str(work / "schema.json"), "-o", str(work / "result.json")]
    for feature in SEALED_FEATURES if sealed else READ_ONLY_FEATURES:
        args += ["--disable", feature]
    if sealed:
        args += ["-c", 'web_search="disabled"', "-c", "project_doc_max_bytes=0"]
        args += ["-c", f"model_catalog_json={toml_string(str(work / 'catalog-sealed.json'))}"]
    args += ["-c", f"model_reasoning_effort={toml_string(request.effort)}"]
    args += ["-c", f"developer_instructions={toml_string(request.profile_prompt)}"]
    return [*args, "-m", request.model, "--", request.user_input]


def sealed_catalog(listing: str) -> dict:
    catalog = json.JSONDecoder().raw_decode(listing, listing.index("{"))[0]
    for entry in catalog["models"]:
        for key in CATALOG_DROPPED:
            entry.pop(key, None)
        entry["experimental_supported_tools"] = []
        entry["supports_search_tool"] = False
    return catalog


def route(environ: Mapping[str, str]) -> tuple[codex_router.CodexAccount, dict[str, str]]:
    account, _, _, _ = codex_router._route(environ, route="", run=subprocess.run, exclude="")
    account = codex_router.credential(account, environ)
    seed = {name: environ[name] for name in (*KEPT, account.env_name, account.key_env) if name in environ}
    return account, codex_router.child_environment(account, seed)


def invoke(request: Request, routed: tuple[codex_router.CodexAccount, dict[str, str]], work: Path) -> str:
    account, child = routed
    home = work / "codex-home"
    home.mkdir()
    env = {**child, "CODEX_HOME": str(home)}
    (work / "schema.json").write_text(json.dumps(request.schema))
    if request.clamp == SEALED:
        listing = execute(codex_router.command(account, CODEX, ["debug", "models"]), env).stdout
        (work / "catalog-sealed.json").write_text(json.dumps(sealed_catalog(listing)))
    execute(codex_router.command(account, CODEX, arguments(request, work)), env)
    result = work / "result.json"
    return result.read_text() if result.exists() else ""
