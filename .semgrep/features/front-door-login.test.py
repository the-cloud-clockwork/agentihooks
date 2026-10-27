# ruleid: feature-front-door-route-check
def _agent_command(spec, report, name, agent_args, environ, directory):
    command = ["agentihooks", "claude", *agent_args]
    before = ""
    return _profile_command(command, spec), before


# ok: feature-front-door-route-check
def _agent_command(spec, report, name, agent_args, environ, directory):
    command = ["agentihooks", "claude", *agent_args]
    command = [*command[:2], *front_door_route.check(spec.profile, spec.title or name, command[2:])]
    before = ""
    return _profile_command(command, spec), before


def launch_claude(binary, args, environ, report):
    # ruleid: feature-front-door-no-token
    environ["AH_CC_TOKEN_worker"] = credential
    # ruleid: feature-front-door-no-token
    return login.launch_token(binary, args, environ, report)


def check(profile, name, args):
    # ruleid: feature-front-door-no-token
    return ["--env", "CLAUDE_CODE_OAUTH_TOKEN", credential]

def _front(environ, cwd):
    # ruleid: feature-front-door-no-token
    return login.launch_token(binary, args, environ, report)


def launch_claude(binary, args, environ, report):
    # ok: feature-front-door-no-token
    return execute("claude", found, child, invoke)


class BadRuntime:
    # ruleid: feature-front-door-runtime-check
    def spawn(self, config, lane, name, task):
        route_kind = self._route_kind(lane, task)
        return route_kind

    # ruleid: feature-front-door-runtime-check
    def resume(self, config, agent, text):
        return self._launch(config, agent, text)


class GoodRuntime:
    # ok: feature-front-door-runtime-check
    def spawn(self, config, lane, name, task):
        forced = front_door_route.check(profile, title, [])
        route_kind = self._route_kind(lane, task, forced)
        route = forced or ["--route", "interactive"]
        return route_kind, route

    # ok: feature-front-door-runtime-check
    def resume(self, config, agent, text):
        forced = front_door_route.check(agent.profile, title, [])
        route = forced or ["--route", "interactive"]
        return route
