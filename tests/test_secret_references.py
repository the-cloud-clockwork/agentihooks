import json

import pytest

from hooks.secrets import contains_generic_secret, redact, scan


def _arn(
    resource: tuple[str, ...],
    service: str = "secretsmanager",
    partition: str = "aws",
    region: str = "us-east-1",
    account: str = "123456789012",
) -> str:
    return ":".join(("arn", partition, service, region, account, *resource))


@pytest.mark.feature("guardrails.secret-reference-literals")
@pytest.mark.parametrize("mode", ["standard", "strict"])
@pytest.mark.parametrize("style", ["single", "double", "json"])
@pytest.mark.parametrize(
    "reference",
    [
        "example/service/credential",
        "/example/service/credential-v2",
        _arn(("secret", "example/service-AbCdEf")),
        _arn(("parameter/example/service/credential",), service="ssm"),
        _arn(("secret", "example/service-AbCdEf"), partition="aws-cn", region="cn-north-1"),
        _arn(
            ("parameter/example/service/credential",),
            service="ssm",
            partition="aws-us-gov",
            region="us-gov-west-1",
        ),
    ],
)
def test_secret_references_remain_visible(reference: str, style: str, mode: str) -> None:
    key = "SEC" + "RET"
    if style == "json":
        text = json.dumps({key: reference})
    else:
        quote = "'" if style == "single" else '"'
        text = f"{key} = {quote}{reference}{quote}"
    assert scan(text, mode=mode) == []
    assert redact(text, mode=mode) == text
    assert not contains_generic_secret(text)


@pytest.mark.feature("guardrails.secret-reference-literals")
@pytest.mark.parametrize("mode", ["standard", "strict"])
def test_resource_literals_remain_visible(mode: str) -> None:
    reference = _arn(("secret", "example/service-AbCdEf"))
    text = f'RESOURCE = "{reference}"'
    assert scan(text, mode=mode) == []
    assert redact(text, mode=mode) == text
    assert not contains_generic_secret(text)


@pytest.mark.feature("guardrails.secret-reference-literals")
@pytest.mark.parametrize("mode", ["standard", "strict"])
@pytest.mark.parametrize("style", ["quoted", "bare", "json"])
@pytest.mark.parametrize(
    "value",
    [
        "7fJk29sLq0pXv8Rt",
        "hunter2hunter2",
        "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        "YWJjZGVm/L2doaWpr==",
        "example/service/credential!",
        "example/service/credential extra",
        "example/service/credential\nextra",
        "example//credential",
        _arn(("secret", "example/service"), account="12345678901"),
        _arn(("secret", "example/service"), region=""),
        _arn(("parameter/example/service",)),
        _arn(("secret", "example/service"), service="ssm"),
        _arn(("parameter/",), service="ssm"),
        _arn(("example/service",), service="s3"),
        _arn(("parameter/example/service",), service="ssm", partition="other"),
        _arn(("parameter/example/service",), service="ssm", region="not-a-region"),
    ],
)
def test_credentials_and_malformed_references_stay_blocked(value: str, style: str, mode: str) -> None:
    key = "SEC" + "RET"
    if style == "json":
        text = json.dumps({key: value})
    elif style == "quoted":
        text = f'{key} = "{value}"'
    else:
        text = f"{key} = {value}"
    assert "generic_secret" in scan(text, mode=mode)
    assert "[REDACTED:generic_secret]" in redact(text, mode=mode)
    assert contains_generic_secret(text)


@pytest.mark.feature("guardrails.secret-reference-literals")
def test_reference_does_not_hide_a_credential_or_provider_token() -> None:
    key = "SEC" + "RET"
    reference = f'{key} = "example/service/credential"'
    value = "7fJk29sLq0pXv8Rt"
    token = "ghp_" + "a" * 36
    text = f'{reference}\n{key} = "{value}"\n{token}'
    assert scan(text, mode="standard") == ["github_token", "generic_secret"]
    assert redact(text, mode="standard") == (f'{reference}\n[REDACTED:generic_secret]"\n[REDACTED:github_token]')
    assert contains_generic_secret(text)


@pytest.mark.feature("guardrails.secret-reference-literals")
@pytest.mark.parametrize("ending", ["", "'", ' extra"', '\nextra"'])
def test_unterminated_reference_is_still_a_literal(ending: str) -> None:
    text = "SEC" + f'RET = "example/service/credential{ending}'
    assert scan(text, mode="standard") == ["generic_secret"]
    assert "[REDACTED:generic_secret]" in redact(text, mode="standard")
    assert contains_generic_secret(text)
