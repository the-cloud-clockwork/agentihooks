# ruleid: shipped-code-imports-tests
from tests import dep_manifest

# ruleid: shipped-code-imports-tests
from tests.queue_shards import SLOWDOWN

# ruleid: shipped-code-imports-tests
import tests.shards

# ruleid: shipped-code-imports-tests
import tests.shards as shards

# ruleid: shipped-code-imports-tests
from tests import coverage_check, reused_coverage

# ok: shipped-code-imports-tests
from scripts import coverage_check as checked, reused_coverage as reused

# ok: shipped-code-imports-tests
from scripts import dep_manifest as manifest

# ok: shipped-code-imports-tests
from scripts.shard_sizing import FIRST_SHARD_FILES

# ok: shipped-code-imports-tests
import testsuite


def lazy() -> object:
    # ruleid: shipped-code-imports-tests
    from tests.test_collection_imports import SDK_GROUP

    return SDK_GROUP


print(dep_manifest, SLOWDOWN, tests.shards, shards, coverage_check, reused_coverage, checked, reused)
print(manifest, FIRST_SHARD_FILES, testsuite, lazy)
