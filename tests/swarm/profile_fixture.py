import json


def validated(argv, output):
    if "--profile" not in argv:
        return output
    result = {
        "state": "validated",
        "profile": argv[argv.index("--profile") + 1],
        "harness": argv[argv.index("--agent") + 1],
        "pid": 123,
        "home": "/fixture/profile",
        "persona": "a" * 64,
        "sources": "b" * 64,
        "revisions": {},
        "source_blobs": [],
    }
    return output + f"profile_validation=validated\nprofile_binding={json.dumps(result)}\n"
