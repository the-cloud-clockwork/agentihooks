from pathlib import Path

import yaml


def langfuse_env(profile_dirs: list[tuple[str, Path]], settings_profile: Path | None = None) -> dict[str, str]:
    enabled = True
    tracing = None
    directories = [directory for _, directory in profile_dirs]
    if settings_profile is not None:
        directories.append(settings_profile)
    for directory in directories:
        manifest = directory / "profile.yml"
        if not manifest.exists():
            continue
        data = yaml.safe_load(manifest.read_text()) or {}
        otel = data.get("otel", {})
        enabled = otel.get("enabled", enabled)
        tracing = otel.get("langfuse", {}).get("enabled", tracing)
    if tracing is not None or not enabled:
        return {"AGENTIHOOKS_LANGFUSE_ENABLED": str(int(bool(enabled and tracing)))}
    return {}


def apply_langfuse_env(settings: dict, profiles: list[tuple[str, Path]], overlay: Path | None) -> None:
    settings.setdefault("_agentihooks", {}).setdefault("env", {}).update(langfuse_env(profiles, overlay))


def apply_collector_env(settings: dict) -> None:
    env = settings.get("env", {})
    aliases = {
        "OTEL_EXPORTER_OTLP_ENDPOINT": "AGENTIHOOKS_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_PROTOCOL": "AGENTIHOOKS_OTLP_PROTOCOL",
    }
    mirrored = {alias: env[name] for name, alias in aliases.items() if env.get(name)}
    settings.setdefault("_agentihooks", {}).setdefault("env", {}).update(mirrored)


def installed_langfuse_env(target: str) -> dict[str, str]:
    from scripts import install

    record = install._global_record(install._load_state(), target)
    names = [name.strip() for name in record.get("profile", "").split(",") if name.strip()]
    if record.get("settings_profile"):
        names.append(record["settings_profile"])
    directories = [(name, directory) for name in names if (directory := install._resolve_profile_dir(name)) is not None]
    return langfuse_env(directories)
