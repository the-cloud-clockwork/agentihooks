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


def serve(environ: dict) -> object:
    # ruleid: lane-spokes-skip-root-hive
    if environ.get(workstation.DEPLOYMENT_ENV) != workstation.DISTRIBUTED:
        return None
    return start(environ)


def _daemon(command: str) -> int:
    def run() -> object:
        # ruleid: lane-spokes-skip-root-hive
        if not workstation.runs_engine(os.environ):
            lanes.serve(os.environ)
        return daemon.run()

    return run()


def _daemon(command: str) -> int:
    def run() -> object:
        daemon.fill_operator_env()
        # ok: lane-spokes-skip-root-hive
        lanes.serve(os.environ)
        return daemon.run()

    return run()
