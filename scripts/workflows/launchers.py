import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from tempfile import mkdtemp

from scripts.headless.profile_run import ProfileRun, execute, run_profile
from scripts.headless.request import TIMEOUT_S
from scripts.workflows.runner import StageResult
from scripts.workflows.schema import Stage

__all__ = ["HeadlessProfile", "Scout"]

SCOUTER = "scouter"
EVIDENCE = "evidence.json"


def attempt_folder(stage: Stage, run_dir: Path) -> Path:
    """A fresh folder for one launch of the stage under the run folder's outputs."""
    artifacts = run_dir / "outputs" / stage.id
    artifacts.mkdir(parents=True, exist_ok=True)
    return Path(mkdtemp(dir=artifacts))


@dataclass(frozen=True)
class Scout:
    harness: str = "auto"
    timeout: float = TIMEOUT_S

    def launch(self, stage: Stage, inputs: dict, run_dir: Path) -> StageResult:
        """Run the headless scout once per count and pass when every run wrote its evidence set."""
        attempt = attempt_folder(stage, run_dir)
        question = json.dumps(inputs)
        folders = [attempt / str(index) for index in range(stage.count)]
        with ThreadPoolExecutor(max_workers=stage.count) as pool:
            outputs = list(pool.map(lambda folder: self.scout(stage, question, folder), folders))
        outcome = "pass" if all(output is not None for output in outputs) else "retry"
        return StageResult(outcome, outputs)

    def scout(self, stage: Stage, question: str, folder: Path) -> dict | None:
        """Run one scout into the folder and return its evidence set, or None when it failed."""
        folder.mkdir(parents=True)
        out = folder / EVIDENCE
        argv = ["agentihooks", "scout", question, "--rounds", str(stage.iterations), "--tier", stage.tier]
        argv += ["--harness", self.harness, "--out", str(out)]
        if stage.exit:
            argv += ["--until", stage.exit]
        if execute(argv, dict(os.environ), folder, self.timeout) != 0 or not out.is_file():
            return None
        return json.loads(out.read_text())


@dataclass(frozen=True)
class HeadlessProfile:
    harness: str = "auto"
    timeout: float = TIMEOUT_S

    def launch(self, stage: Stage, inputs: dict, run_dir: Path) -> StageResult:
        if stage.profile == SCOUTER:
            return Scout(self.harness, self.timeout).launch(stage, inputs, run_dir)
        attempt = attempt_folder(stage, run_dir)
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
