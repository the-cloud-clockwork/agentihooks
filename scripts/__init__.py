import os

from scripts import gh_config

gh_config.pin(os.environ)
if os.environ.get("GITHUB_ACTIONS") != "true":
    from scripts import gh_audit

    gh_audit.install()
