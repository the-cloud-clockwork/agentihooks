def serve(environ: dict) -> object:
    # ruleid: lane-spokes-skip-root-hive
    if workstation.runs_engine(environ):
        return None
    return start(environ)


def serve_bare(environ: dict) -> object:
    return start(environ)


def serve(environ: dict) -> object:
    # ruleid: lane-spokes-skip-root-hive
    if runs_engine(environ):
        return None
    return start(environ)


def serve(environ: dict) -> object:
    # ruleid: lane-spokes-skip-root-hive
    if environ.get(DEPLOYMENT_ENV) != DISTRIBUTED:
        return None
    return start(environ)


def serve(environ: dict) -> object:
    # ok: lane-spokes-skip-root-hive
    if (environ.get(BACKEND_ENV) or DEFAULT_BACKEND) == "memory":
        return None
    return start(environ)


def tick(environ: dict) -> object:
    # ok: lane-spokes-skip-root-hive
    if workstation.runs_engine(environ):
        return None
    return run(environ)
