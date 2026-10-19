"""
Allow running hook_manager as a module.

Usage:
    python -m hooks.hook_manager
    python -m hooks
"""

from hooks import hook_manager
from hooks.prompt_budget import process_start

if __name__ == "__main__":
    hook_manager._EVENT_STARTED = process_start()
    hook_manager.main()
