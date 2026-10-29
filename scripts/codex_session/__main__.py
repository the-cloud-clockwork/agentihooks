import sys

if sys.argv[1:2] == ["attach"]:
    from scripts.codex_session.attach import main as attach

    raise SystemExit(attach(sys.argv[2:]))

from scripts.codex_session.serve import main

raise SystemExit(main())
