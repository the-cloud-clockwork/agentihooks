# Eval transcript: scaffold one step

Run on 2026-11-05 in a throwaway checkout of `origin/dev` at ea2e4f6cc6, base tree a second `origin/dev` checkout.

```text
$ python -m scripts.tick.new_step eval_probe --group observe --lane tick --interval 60 --effects inbox --feature swarm-tick-characterisation --module observe
scripts/tick/steps/observe.py
scripts/tick/steps/budgets.py
tests/tick/steps/test_eval_probe.py
tests/swarm/tick_scenarios/steps.json
feature-map.json
exit 0
$ python -m scripts.tick.new_step --check eval_probe
{
 "step": true,
 "budget row": true,
 "test": true,
 "index row": true,
 "feature entry": true
}
exit 0
$ python -m scripts.tick_pattern --base "$BASE"
513 tick pattern offences in 8 files, 513 on the base allowlist, 0 errors
exit 0
$ python -m scripts.tick.catalogue check
0 tick catalogue errors
exit 0
$ python -m scripts.feature_map
Feature map: 126 features, all rule, test and live check links exist
exit 0
$ git status --short
 M feature-map.json
 M scripts/tick/steps/budgets.py
 M scripts/tick/steps/observe.py
 M tests/swarm/tick_scenarios/steps.json
?? tests/tick/steps/test_eval_probe.py
```

The floor row written: `"eval_probe": Timing(LIGHT_S, CRITICAL_S),`.
