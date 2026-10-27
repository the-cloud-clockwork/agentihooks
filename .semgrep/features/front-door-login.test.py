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
