def single() -> object:
    # ruleid: test-private-imports
    from tests.swarm.test_runtime import _spawn_env

    return _spawn_env


def several() -> object:
    # ruleid: test-private-imports
    from tests.test_profile_render import _commit, _write

    return _commit, _write


def parenthesised() -> object:
    from tests.ci_lab.test_scheduler import (
        BASE,
        # ruleid: test-private-imports
        _gate,
    )

    return BASE, _gate


def relative() -> object:
    # ruleid: test-private-imports
    from .test_ledger_definitions import _duplicates

    return _duplicates


def aliased() -> object:
    # ruleid: test-private-imports
    from tests.swarm.test_create_refusal import _args as create_args

    return create_args


def package() -> object:
    # ruleid: test-private-imports
    from tests import _shared

    return _shared


def private_alias() -> object:
    # ok: test-private-imports
    from tests.ci_lab.test_scheduler import lab as _lab

    return _lab


def public() -> object:
    # ok: test-private-imports
    from tests.conftest import patch_swarm_store_connect

    return patch_swarm_store_connect


def shipped() -> object:
    # ok: test-private-imports
    from scripts.swarm.runtime import _spawn

    return _spawn


def dunder() -> object:
    # ok: test-private-imports
    from tests.helpers import __all__

    return __all__


def module() -> object:
    # ok: test-private-imports
    import tests.count_floor as count_floor

    return count_floor
