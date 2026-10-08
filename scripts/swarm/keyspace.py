"""Root of every swarm Redis key, read once at import. AGENTIHOOKS_SWARM_KEY_PREFIX moves the swarm stores off the
production key names; the test suite sets it before any store is imported."""

import os

ENV = "AGENTIHOOKS_SWARM_KEY_PREFIX"
PRODUCTION = "agentihooks"
ROOT = os.environ.get(ENV) or PRODUCTION
