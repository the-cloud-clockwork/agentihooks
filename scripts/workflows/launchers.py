import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from tempfile import mkdtemp

from scripts.headless.profile_run import ProfileRun, run_profile
from scripts.headless.request import TIMEOUT_S
from scripts.workflows.runner import StageResult
from scripts.workflows.schema import Stage

__all__ = ["HeadlessProfile"]


@dataclass(frozen=True)
class HeadlessProfile:
    harness: str = "auto"
    timeout: float = TIMEOUT_S

    def launch(self, stage: Stage, inputs: dict, run_dir: Path) -> StageResult:
        artifacts = run_dir / "outputs" / stage.id
        artifacts.mkdir(parents=True, exist_ok=True)
        attempt = Path(mkdtemp(dir=artifacts))
        requests = [
            ProfileRun(
                profile=stage.profile,
                harness=self.harness,
                input=json.dumps(inputs),
                schema=None,
                tier=stage.tier,
                effort=stage.effort,
                timeout=self.timeout,
                run_dir=attempt / str(index),
            )
            for index in range(stage.count)
        ]
        with ThreadPoolExecutor(max_workers=stage.count) as pool:
            results = list(pool.map(run_profile, requests))
        outcome = "pass" if all(result.status == "ok" for result in results) else "retry"
        return StageResult(outcome, [result.output for result in results])
