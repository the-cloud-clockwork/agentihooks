# Swarm v2 implementation baseline

Package SV2-FND-01. Observed at 2026-10-07T19:37:44Z.
Source is the branch head. Deployed values come from read-only probes.
An unverified value is unknown: it never means empty, absent or zero.
baseline_drift_items counts drift found by this run; the Drift section keeps every earlier entry.
Regenerate with `python -m scripts.swarm_v2.baseline --sources docs/swarm-v2/baseline-sources.json --previous docs/swarm-v2/baseline.json --json docs/swarm-v2/baseline.json --markdown docs/swarm-v2/baseline.md`.

| Repository | Source | Deployed | Unknown live values |
|---|---|---|---|
| agentihooks | dev `2bfd09c2f23dbb54c59baaba1cb0c1948fb1f55d` | workstation agentihooks install: verified `agentihooks 2.17.0` | swarm controller on Anton (not deployed yet), editable checkout revision serving the shared ledger server |
| agentibrain-kernel | dev `e58e687e8c9edc6acab8115e7eb15db302b704dc` | brain-api version: unverified (endpoint answered without version) | brain-api image digest on its compose host, brain-api version |
| antoncore | dev `0edc7cdf2edf154e879db6d8b41c2bdb92e65c8c` | ArgoCD synced revision: verified `0edc7cdf2edf154e879db6d8b41c2bdb92e65c8c`, matches source; cluster autoscaler image: verified `registry.k8s.io/autoscaling/cluster-autoscaler:v1.33.0` | live autoscaling group desired capacity and instance counts (AWS read access not exercised) |

## Source-proven interfaces

- agentihooks `CLAUDE.md`: present
- agentihooks `scripts/swarm/store.py`: present
- agentihooks `scripts/swarm/runtime.py`: present
- agentihooks `scripts/terminate_agent.py`: present
- agentihooks `scripts/swarm_ledger/ledger.py`: present
- agentihooks `hooks/context/project_identity.py`: present
- agentihooks `hooks/memory/transcript_reader.py`: present
- agentihooks `hooks/targets/normalizer.py`: present
- agentihooks `scripts/swarm/snapshot.py`: present
- agentihooks `hooks/memory/store.py`: present
- agentihooks `hooks/context/project_cache.py`: present
- agentibrain-kernel `services/mcp/app/tools/arcs.py`: present
- agentibrain-kernel `docs/CLI.md`: present
- agentibrain-kernel `agentibrain/config.py`: present
- agentibrain-kernel `agentibrain/templates/compose/compose.yml.j2`: present
- agentibrain-kernel `services/brain-ops/extract.py`: present
- agentibrain-kernel `services/brain-ops/cluster.py`: present
- agentibrain-kernel `services/brain-api/app/feed.py`: present
- agentibrain-kernel `services/brain-ops/brain_apply.py`: present
- agentibrain-kernel `services/brain-ops/embed_arcs.py`: present
- agentibrain-kernel `services/brain-ops/brain_tick_prompt.py`: present
- antoncore `stacks/terraform/modules/ec2-arc-runners/asg.tf`: present
- antoncore `stacks/terraform/modules/ec2-arc-runners/variables.tf`: present
- antoncore `k8s/charts/cluster-autoscaler/values.yaml`: present
- antoncore `automation/packer/files/anton-k3s-join.sh`: present

## Drift

- 2026-10-07T19:28:08Z agentihooks: `e440d3fb7bdd0491c8896ca528440041a43c36c4` to `d074deceeeb0ed58a16eccd15acf2165982385bb`
- 2026-10-07T19:28:08Z agentibrain-kernel: `80174260fee601659b94b029b9a5a58ee8e1ef04` to `e58e687e8c9edc6acab8115e7eb15db302b704dc`
- 2026-10-07T19:28:08Z antoncore: `94b2a670daf11eeed02b853b08b3b29c451a6b13` to `0edc7cdf2edf154e879db6d8b41c2bdb92e65c8c`
- 2026-10-07T19:37:44Z agentihooks: `d074deceeeb0ed58a16eccd15acf2165982385bb` to `2bfd09c2f23dbb54c59baaba1cb0c1948fb1f55d`

## Measurements

- baseline_unverified_items: 1
- baseline_drift_items: 1
