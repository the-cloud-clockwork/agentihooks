class NoAdmittedId:
    def _claim(self, agent, doc, task):
        from scripts.swarm import profile_choice

        # ruleid: admission-identity-skips-admitted-execution
        identity = {
            "execution_id": agent.execution_id,
            "execution_generation": agent.generation,
            profile_choice.LEDGER_INTENT: profile_choice.carried(doc, task),
        }
        return identity


class CarriesAdmittedId:
    def _claim(self, agent, doc, task):
        from scripts.swarm import profile_choice

        # ok: admission-identity-skips-admitted-execution
        identity = {
            "execution_id": agent.execution_id,
            "admitted_execution_id": agent.execution_id,
            "execution_generation": agent.generation,
            profile_choice.LEDGER_INTENT: profile_choice.carried(doc, task),
        }
        return identity
