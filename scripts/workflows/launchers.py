import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from tempfile import mkdtemp
from typing import Any, TypeGuard

from scripts.headless.profile_run import ProfileRun, execute, run_profile
from scripts.headless.request import TIMEOUT_S
from scripts.workflows.runner import StageLauncher, StageResult
from scripts.workflows.schema import CASE, Stage

__all__ = ["HeadlessProfile", "Judge", "Routed", "Scout"]

SCOUTER = "scouter"
EVIDENCE = "evidence.json"
JUDGE = ("agentihooks", "judge")
CASE_FILE = "case.json"
EVIDENCE_FILE = "evidence.json"
VERDICT_FILE = "verdict.json"


def attempt_folder(stage: Stage, run_dir: Path) -> Path:
    """A fresh folder for one launch of the stage under the run folder's outputs."""
    artifacts = run_dir / "outputs" / stage.id
    artifacts.mkdir(parents=True, exist_ok=True)
    return Path(mkdtemp(dir=artifacts))


def evidence_items(inputs: dict[str, Any]) -> list[Any]:
    """Every evidence item in the earlier stage outputs, in input order."""
    return [
        item
        for outputs in inputs.values()
        for output in outputs
        if is_record(output)
        for item in output.get("items", [])
    ]


def is_record(output: object) -> TypeGuard[dict[str, list[Any]]]:
    """Whether a stage output is a record that can carry evidence items."""
    return isinstance(output, dict)


@dataclass(frozen=True)
class Scout:
    harness: str = "auto"
    timeout: float = TIMEOUT_S

    def launch(self, stage: Stage, inputs: dict[str, Any], run_dir: Path) -> StageResult:
        """Run the headless scout once per count and pass when every run wrote its evidence set."""
        attempt = attempt_folder(stage, run_dir)
        folders = [attempt / str(index) for index in range(stage.count)]
        with ThreadPoolExecutor(max_workers=stage.count) as pool:
            outputs = list(pool.map(partial(self.scout, stage, json.dumps(inputs)), folders))
        outcome = "pass" if all(output is not None for output in outputs) else "retry"
        return StageResult(outcome, outputs)

    def scout(self, stage: Stage, question: str, folder: Path) -> dict[str, Any] | None:
        """Run one scout into the folder and return its evidence set, or None when it failed."""
        folder.mkdir()
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

    def launch(self, stage: Stage, inputs: dict[str, Any], folder: Path) -> StageResult:
        if stage.filters:
            raise ValueError(f"{stage.id} carries filters and runs only through agentihooks judge")
        if stage.profile == SCOUTER:
            return Scout(self.harness, self.timeout).launch(stage, inputs, folder)
        attempt = attempt_folder(stage, folder)
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

    def launch(self, stage: Stage, inputs: dict[str, Any], folder: Path) -> StageResult:
        attempt = attempt_folder(stage, folder)
        case = attempt / CASE_FILE
        evidence = attempt / EVIDENCE_FILE
        verdict = folder / VERDICT_FILE
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

    def launch(self, stage: Stage, inputs: dict[str, Any], folder: Path) -> StageResult:
        launcher = self.judge if stage.filters else self.profile
        return launcher.launch(stage, inputs, folder)
