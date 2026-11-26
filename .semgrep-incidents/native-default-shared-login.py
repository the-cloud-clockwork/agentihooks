def shared(home, ordinary):
    # ruleid: native-default-shared-login
    (home / "auth.json").symlink_to(ordinary / "auth.json")
    # ruleid: native-default-shared-login
    (home / ".credentials.json").symlink_to(ordinary / ".credentials.json")


def isolated(home, ordinary, copy):
    # ok: native-default-shared-login
    copy(ordinary / "auth.json", home / "auth.json")
    # ok: native-default-shared-login
    copy(ordinary / ".credentials.json", home / ".credentials.json")
