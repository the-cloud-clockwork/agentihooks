from scripts.gates.entry import main

# ok: feature-expert-role-stop-condition
raise SystemExit(main(["expert-stop"]))

# ruleid: feature-expert-role-stop-condition
raise SystemExit(main(["claim-stop"]))

# ruleid: feature-expert-role-stop-condition
raise SystemExit(main(["expert-stop", "claim-stop"]))

# ruleid: feature-expert-role-stop-condition
raise SystemExit(main([]))
