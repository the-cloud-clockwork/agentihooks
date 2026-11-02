import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from tempfile import mkdtemp

from scripts.headless.profile_run import ProfileRun, run_profile
from scripts.headless.request import TIMEOUT_S
from scripts.workflows.runner import StageLauncher, StageResult
from scripts.workflows.schema import CASE, Stage

__all__ = ["HeadlessProfile", "Judge", "Routed"]

JUDGE = ("agentihooks", "judge")
CASE_FILE = "case.json"
EVIDENCE_FILE = "evidence.json"
VERDICT_FILE = "verdict.json"


def attempt_folder(stage: Stage, run_dir: Path) -> Path:
    """A fresh folder under outputs/<stage> for one launch."""
    artifacts = run_dir / "outputs" / stage.id
    artifacts.mkdir(parents=True, exist_ok=True)
    return Path(mkdtemp(dir=artifacts))


def evidence_items(inputs: dict) -> list:
    """Every evidence item in the earlier stage outputs, in input order."""
    return [
        item
        for key, outputs in inputs.items()
        if key != CASE
        for output in outputs
        if isinstance(output, dict)
        for item in output.get("items", [])
    ]


@dataclass(frozen=True)
class HeadlessProfile:
    harness: str = "auto"
    timeout: float = TIMEOUT_S

    def launch(self, stage: Stage, inputs: dict, run_dir: Path) -> StageResult:
        if stage.filters:
            raise ValueError(f"{stage.id} carries filters and runs only through agentihooks judge")
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


@dataclass(frozen=True)
class Judge:
    harness: str = "auto"
    timeout: float = TIMEOUT_S

    def launch(self, stage: Stage, inputs: dict, run_dir: Path) -> StageResult:
        attempt = attempt_folder(stage, run_dir)
        case = attempt / CASE_FILE
        evidence = attempt / EVIDENCE_FILE
        verdict = run_dir / VERDICT_FILE
        case.write_text(inputs[CASE])
        evidence.write_text(json.dumps({"items": evidence_items(inputs)}))
        command = [
            *JUDGE,
            "--case",
            str(case),
            "--evidence",
            str(evidence),
            "--tier",
            stage.tier,
            "--harness",
            self.harness,
            "--out",
            str(verdict),
        ]
        try:
            done = subprocess.run(command, capture_output=True, text=True, timeout=self.timeout, check=False)
        except subprocess.TimeoutExpired:
            return StageResult(
                "retry", {"exit": None, "stdout": "", "stderr": f"judge timed out after {self.timeout} seconds"}
            )
        if done.returncode != 0:
            return StageResult("retry", {"exit": done.returncode, "stdout": done.stdout, "stderr": done.stderr})
        record = json.loads(verdict.read_text())
        return StageResult(record["verdict"], record)


@dataclass(frozen=True)
class Routed:
    profile: StageLauncher
    judge: StageLauncher

    def launch(self, stage: Stage, inputs: dict, run_dir: Path) -> StageResult:
        launcher = self.judge if stage.filters else self.profile
        return launcher.launch(stage, inputs, run_dir)
