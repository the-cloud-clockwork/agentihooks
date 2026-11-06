class Swarm:
    @property
    def naming(self):
        from scripts.swarm import naming

        return naming

    @property
    def store(self):
        from scripts.swarm import store

        return store

    @property
    def commands(self):
        from scripts.swarm import commands

        return commands

    @property
    def lifecycle(self):
        from scripts.swarm import lifecycle

        return lifecycle

    @property
    def freeze(self):
        from scripts.swarm import freeze

        return freeze

    @property
    def effort_range(self):
        from scripts.swarm import effort_range

        return effort_range

    @property
    def claim_hold(self):
        from scripts.swarm import claim_hold

        return claim_hold

    @property
    def idle(self):
        from scripts.swarm import idle

        return idle

    @property
    def lease(self):
        from scripts.swarm import lease

        return lease

    @property
    def model_catalog(self):
        from scripts.swarm import model_catalog

        return model_catalog

    @property
    def context_view(self):
        from scripts.swarm import context_view

        return context_view

    @property
    def default_models(self):
        from scripts.swarm import default_models

        return default_models

    @property
    def quota_refresh(self):
        from scripts.swarm import quota_refresh

        return quota_refresh

    @property
    def operator_mail(self):
        from scripts.swarm import operator_mail

        return operator_mail

    @property
    def overlays(self):
        from scripts.swarm import overlays

        return overlays

    @property
    def verdicts(self):
        from scripts.swarm.health import verdicts

        return verdicts

    @property
    def phase_state(self):
        from scripts.swarm import phase_state

        return phase_state

    @property
    def affinity(self):
        from scripts.swarm import affinity

        return affinity

    @property
    def templates(self):
        from scripts.swarm import templates

        return templates

    @property
    def stack_env(self):
        from scripts.swarm import stack_env

        return stack_env

    @property
    def stack_init(self):
        from scripts.swarm import stack_init

        return stack_init

    @property
    def front_door(self):
        from scripts.swarm_v2 import front_door

        return front_door

    @property
    def ledger_writer(self):
        from scripts.swarm_v2 import ledger_writer

        return ledger_writer

    @property
    def master_fence(self):
        from scripts.swarm_v2 import master_fence

        return master_fence

    @property
    def masters(self):
        from scripts.swarm_v2 import masters

        return masters

    @property
    def architecture(self):
        from scripts.swarm_v2 import architecture

        return architecture

    @property
    def operator_auth(self):
        from scripts.swarm_v2 import operator_auth

        return operator_auth

    @property
    def task_specs(self):
        from scripts.swarm_v2.api import tasks

        return tasks

    @property
    def authority(self):
        from scripts.swarm_v2 import authority

        return authority

    @property
    def ledger_cutover(self):
        from scripts.swarm import ledger_cutover

        return ledger_cutover
