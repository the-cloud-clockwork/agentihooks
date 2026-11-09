def premature(report: str, env: dict[str, str]) -> None:
    # ruleid: master-token-route-before-admission
    _write_route_report(report, status="routed")
    execute("claude", "account", env, invoke)


def admitted(report: str, env: dict[str, str]) -> None:
    def invoke(child: dict[str, str]) -> None:
        # ok: master-token-route-before-admission
        _write_route_report(report, status="routed")
        launch(child)

    execute("claude", "account", env, invoke)
