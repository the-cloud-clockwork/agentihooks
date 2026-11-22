def spawn(self, config, lane, name, task):
    picked = replace(picked, effort=saved["effort"]) if saved.get("effort") else picked
    # ruleid: feature-expert-launch-pin
    model = _model_args(agent, picked.__dict__, environ, effort_range.bounds(config, lane))
    return self._account_launch(config, lane, task, name, [*argv, "--", *model])


def spawn(self, config, lane, name, task):
    picked = replace(picked, effort=saved["effort"]) if saved.get("effort") else picked
    picked = expert_launch.pin(lane, picked, agent, task.get(expert_launch.EFFORT_KEY))
    # ok: feature-expert-launch-pin
    model = _model_args(agent, picked.__dict__, environ, effort_range.bounds(config, lane))
    return self._account_launch(config, lane, task, name, [*argv, "--", *model])


def spawn(self, config, lane, name, task):
    picked = replace(picked, effort=saved["effort"]) if saved.get("effort") else picked
    with self._seat_returned_on_failure(account, lane, agent):
        placed = self._account_launch(
            config,
            lane,
            task,
            name,
            [
                *argv,
                "--",
                # ruleid: feature-expert-launch-pin
                *_model_args(agent, picked.__dict__, environ, effort_range.bounds(config, lane)),
            ],
        )
    return placed


def spawn(self, config, lane, name, task):
    picked = replace(picked, effort=saved["effort"]) if saved.get("effort") else picked
    picked = expert_launch.pin(lane, picked, agent, task.get(expert_launch.EFFORT_KEY))
    with self._seat_returned_on_failure(account, lane, agent):
        placed = self._account_launch(
            config,
            lane,
            task,
            name,
            [
                *argv,
                "--",
                # ok: feature-expert-launch-pin
                *_model_args(agent, picked.__dict__, environ, effort_range.bounds(config, lane)),
            ],
        )
    return placed


def spawn(self, config, lane, name, task):
    held = expert_launch.pin(lane, picked, agent, task.get(expert_launch.EFFORT_KEY))
    # ruleid: feature-expert-launch-pin
    model = _model_args(agent, picked.__dict__, environ, effort_range.bounds(config, lane))
    return self._account_launch(config, lane, task, name, [*argv, "--", *model])


def spawn(self, config, lane, name, task):
    expert_launch.pin(lane, picked, agent, task.get(expert_launch.EFFORT_KEY))
    # ruleid: feature-expert-launch-pin
    model = _model_args(agent, picked.__dict__, environ, effort_range.bounds(config, lane))
    return self._account_launch(config, lane, task, name, [*argv, "--", *model])


def spawn(self, config, lane, name, task):
    # ruleid: feature-expert-launch-pin
    model = _model_args(agent, picked.__dict__, environ, effort_range.bounds(config, lane))
    picked = expert_launch.pin(lane, picked, agent, task.get(expert_launch.EFFORT_KEY))
    return self._account_launch(config, lane, task, name, [*argv, "--", *model])


def spawn(self, config, lane, name, task):
    picked = expert_launch.pin(lane, picked, agent, task.get(expert_launch.EFFORT_KEY))
    # ruleid: feature-expert-launch-pin
    model = _model_args(agent, {"model": "opus", "effort": "high"}, environ, effort_range.bounds(config, lane))
    return self._account_launch(config, lane, task, name, [*argv, "--", *model])


def resume(self, config, agent, text):
    picked = model_pick.ModelPick(agent.model, agent.effort)
    # ruleid: feature-expert-launch-pin
    model = _model_args(agent.harness, picked.__dict__, environ, effort_range.bounds(config, agent.lane))
    return self._launch(config, agent.lane, agent.task, agent.name, [*argv, "--", *model])


def resume(self, config, agent, text):
    picked = model_pick.ModelPick(agent.model, agent.effort)
    picked = expert_launch.pin(agent.lane, picked, agent.harness, agent.effort)
    # ok: feature-expert-launch-pin
    model = _model_args(agent.harness, picked.__dict__, environ, effort_range.bounds(config, agent.lane))
    return self._launch(config, agent.lane, agent.task, agent.name, [*argv, "--", *model])


def operator(self, config, name, profile, text):
    picked = model_pick.frontier("claude", default_models.model(config, "claude"))
    # ok: feature-expert-launch-pin
    model = _model_args("claude", picked.__dict__, dict(os.environ), effort_range.of(config))
    return self._launch(config, naming.OPERATOR, "", name, [*argv, "--", *model])
