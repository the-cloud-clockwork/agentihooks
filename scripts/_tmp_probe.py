import subprocess
from collections.abc import Callable
from typing import cast

from scripts.ci_budget.rate_limit import gh_api as _raw_gh_api

gh_api: Callable[..., subprocess.CompletedProcess[bytes]] = cast(
    "Callable[..., subprocess.CompletedProcess[bytes]]", _raw_gh_api
)

result = gh_api(["x"], timeout=1)
print(result.stdout)
