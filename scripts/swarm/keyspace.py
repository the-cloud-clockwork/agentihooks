"""Root of every swarm Redis key. Read once at import, so a caller that moves it sets the variable first."""

import os

ENV = "AGENTIHOOKS_SWARM_KEY_PREFIX"
PRODUCTION = "agentihooks"
ROOT = os.environ.get(ENV) or PRODUCTION
