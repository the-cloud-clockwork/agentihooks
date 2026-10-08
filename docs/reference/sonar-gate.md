# Sonar quality gate

The `sonar` job in the Tests workflow is a need of `Gate — Required`. It scans every pull request, merge queue entry and dev push, then fails when the SonarQube quality gate fails.

agentihooks is bound to the `Delivery L2` gate, defined and bound in antoncore's SonarQube stack config (`stacks/sonarqube/config/gates/delivery-l2.json` and `projects.json`). Its conditions on new code:

| Metric | Fails when |
|---|---|
| Coverage | below 80 % |
| Bugs | above 0 |
| Vulnerabilities | above 0 |
| Duplicated lines | above 3 % |
| Security hotspots reviewed | below 100 % |

The job's `Hold the Delivery L2 conditions` step runs after the gate action and fails when:

- the `Delivery L2` gate on the server holds conditions other than the five above, so a loosened threshold turns the job red;
- the analysis was graded on a condition outside those five, as after a rebind to another gate;
- bugs or vulnerabilities were not graded, or coverage and duplication were not graded on a change Sonar did not mark as small.

Sonar leaves the hotspots condition out of the result when a change has no hotspots, and skips coverage and duplication on a change under 20 new lines. The scan token cannot read the project binding itself (HTTP 403 on `api/qualitygates/get_by_project`), so a rebind to a gate whose conditions are a subset of these five passes the step; the binding is kept by antoncore's `projects.json`, applied on every SonarQube stack deploy. Gate thresholds change only through the antoncore config.

## False positives

An issue is marked false positive or won't fix only through the `sonar-config` skill's `issue-transition` helper, never by hand in the SonarQube UI. Two readers that did not write the flagged code each record their ruling as a comment on the Sonar issue before the transition; the pull request that needs it names both readers and the issue key.
