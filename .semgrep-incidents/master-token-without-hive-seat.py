def launch_token() -> None:
    # ruleid: master-token-without-hive-seat
    execute("claude", "account", {}, launch)
    # ruleid: master-token-without-hive-seat
    execute("claude", "account", {}, launch, master=False)
    # ok: master-token-without-hive-seat
    execute("claude", "account", {}, launch, master=True)


def lane() -> None:
    # ok: master-token-without-hive-seat
    execute("claude", "account", {}, launch)
