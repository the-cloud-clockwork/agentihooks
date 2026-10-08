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

The job's `Hold the Delivery L2 conditions` step reads the conditions the analysis was graded on (`api/qualitygates/project_status`) and fails unless they are exactly the five above, so a rebind or a loosened threshold on the server turns the job red. Sonar skips the coverage and duplication conditions on a change under 20 new lines. Gate thresholds change only through the antoncore config.

## False positives

An issue is marked false positive or won't fix only through the `sonar-config` skill's `issue-transition` helper, never by hand in the SonarQube UI. Two readers that did not write the flagged code each rule on the issue first; the pull request that needs the transition names both readers and the issue key.
