#!/usr/bin/env python3
"""Entry point of the launch-troubleshooting-crew skill; the stages live in scripts.swarm.troubleshooting_crew."""

import sys

from scripts.swarm.troubleshooting_crew import main

if __name__ == "__main__":
    sys.exit(main())
