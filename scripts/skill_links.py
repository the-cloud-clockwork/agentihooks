import os
from pathlib import Path

from scripts import install


def dangling() -> list[Path]:
    ledger = install._state_links()
    homes = {install.CLAUDE_HOME, Path.home() / ".claude"}
    return sorted(
        link
        for home in homes
        if (home / "skills").is_dir()
        for link in (home / "skills").iterdir()
        if link.is_symlink() and not link.exists() and install._link_is_managed(link, ledger)
    )


def repair() -> None:
    with install._sync_lock():
        links = dangling()
        if not links:
            return
        state = install._load_state()
        installed = state.get("targets", {}).get("global", {}).get("claude", {}).get("profile", "default")
        bundle = install._get_bundle_path()
        records = []
        removed = []
        for link in links:
            profile = installed
            if link.parent.parent != Path.home() / ".claude":
                profile = os.environ.get("AGENTIHOOKS_PROFILE", installed)
            roots = [install.PACKAGE_FEATURES_DIR]
            if bundle:
                roots.append(bundle / ".claude")
            roots.extend(directory / ".claude" for _, directory in install._resolve_profile_chain(profile))
            candidates = [root / "skills" / link.name for root in roots]
            current = next((item for item in reversed(candidates) if (item / "SKILL.md").is_file()), None)
            if current is None:
                link.unlink()
                removed.append(link)
            else:
                records.append(install._link_item(current, link, "skill"))
        install._state_forget_links(removed)
        install._state_record_links(records)
