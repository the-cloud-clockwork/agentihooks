import os

from scripts import gh_audit, gh_config

gh_config.pin(os.environ)
gh_audit.install()
