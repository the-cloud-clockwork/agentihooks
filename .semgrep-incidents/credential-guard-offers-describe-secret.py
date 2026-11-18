KIND_ENVIRONMENT = "environment"
# ruleid: credential-guard-offers-describe-secret
DESCRIBE_ALTERNATIVE = "\nkubectl describe secret <name> shows the keys and byte sizes without the values."
# ok: credential-guard-offers-describe-secret
KEY_LISTING = "\nkubectl get secret <name> --template='{{range $k,$v:=.data}}{{$k}} {{end}}'"


def block(what: str, kind: str) -> str:
    return "BLOCKED: " + what + " " + kind


def get_secret_refusal() -> str:
    # ruleid: credential-guard-offers-describe-secret
    return (
        block("printing Secret data (kubectl get secret -o yaml/json)", KIND_ENVIRONMENT)
        + "\nkubectl describe secret <name> shows the keys and byte sizes without the values."
    )


def describe_secret_refusal() -> str:
    # ok: credential-guard-offers-describe-secret
    return block("describing a Secret (kubectl describe secret prints the token)", KIND_ENVIRONMENT) + KEY_LISTING


def key_listing_refusal() -> str:
    # ok: credential-guard-offers-describe-secret
    return (
        block("printing Secret data (kubectl get secret -o yaml/json)", KIND_ENVIRONMENT)
        + "\nkubectl get secret <name> -o go-template lists the key names without the values."
    )
