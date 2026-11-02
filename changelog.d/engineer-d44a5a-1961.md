### Fixed

- Doctor failed spawn detector: an account reservation refusal is judged by the `sessions` and `cap` counted at the refusal, so a refusal at its counted cap is no longer a finding when the tick start capacity snapshot missed live remote sessions.
