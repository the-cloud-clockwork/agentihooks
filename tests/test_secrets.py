"""Tests for hooks.secrets module.

All secret-like test strings are assembled inside each test function so
the source file itself does not match any detection pattern.
"""

import pytest

pytestmark = pytest.mark.unit


class TestScan:
    """Tests for secrets.scan()."""

    def test_scan_aws_access_key(self):
        key = "AKIA" + "IOSFODNN7EXAMPLE"
        from hooks.secrets import scan

        hits = scan(f"export AWS_ACCESS_KEY_ID={key}")
        assert "aws_access_key" in hits

    def test_scan_github_token(self):
        token = "ghp_" + "A" * 36
        from hooks.secrets import scan

        hits = scan(f"token: {token}")
        assert "github_token" in hits

    def test_scan_private_key(self):
        header = "-----BEGIN RSA" + " PRIVATE KEY-----"
        from hooks.secrets import scan

        hits = scan(f"{header}\nMIIEowIBAAK...")
        assert "private_key" in hits

    def test_scan_db_url(self):
        scheme = "postgres"
        userpass = "admin:s3cr3tpassword"
        host = "db.example.com/mydb"
        url = f"{scheme}://{userpass}@{host}"
        from hooks.secrets import scan

        hits = scan(url)
        assert "db_url_creds" in hits

    def test_scan_clean_text(self):
        from hooks.secrets import scan

        hits = scan("ls -la /tmp && echo 'hello world' && git status")
        assert hits == []

    def test_scan_env_var_reference(self):
        """PASSWORD=$MY_VAR should not be flagged — it is a reference, not a value."""
        from hooks.secrets import scan

        hits = scan("PASSWORD=$MY_VAR")
        assert "generic_secret" not in hits

    def test_scan_env_var_reference_placeholder(self):
        """PASSWORD=<placeholder> should not be flagged."""
        from hooks.secrets import scan

        hits = scan("PASSWORD=<my-secret-here>")
        assert "generic_secret" not in hits

    def test_scan_bearer_token(self):
        header = "Authorization: Bearer "
        token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" + ".payload.sig"
        from hooks.secrets import scan

        hits = scan(header + token)
        assert "bearer_token" in hits

    def test_scan_aws_secret_key(self):
        key_name = "aws_secret" + "_access_key"
        key_val = "wJalrXUtnFEMI" + "/K7MDENG/bPxRfiCYEXAMPLEKEY"
        from hooks.secrets import scan

        hits = scan(f"{key_name}={key_val}")
        assert "aws_secret_key" in hits

    def test_scan_generic_secret_value(self):
        """Literal secret value assigned to a known key name should be flagged."""
        name = "API" + "_KEY"
        value = "supersecretvalue123"
        from hooks.secrets import scan

        hits = scan(f"{name}={value}")
        assert "generic_secret" in hits

    def test_scan_nosecret_suppresses_line(self):
        """A line ending with # nosecret should not be flagged."""
        key = "AKIA" + "IOSFODNN7EXAMPLE"
        from hooks.secrets import scan

        hits = scan(f"key = {key}  # nosecret")
        assert hits == []

    def test_scan_nosecret_only_suppresses_marked_line(self):
        """Only the marked line is suppressed; other lines are still scanned."""
        key = "AKIA" + "IOSFODNN7EXAMPLE"
        from hooks.secrets import scan

        text = f"safe = 'nothing'  # nosecret\nkey = {key}\n"
        hits = scan(text)
        assert "aws_access_key" in hits

    def test_scan_nosecret_case_insensitive(self):
        """# NOSECRET and # NoSecret are also valid suppressors."""
        key = "AKIA" + "IOSFODNN7EXAMPLE"
        from hooks.secrets import scan

        assert scan(f"x = {key}  # NOSECRET") == []
        assert scan(f"x = {key}  # NoSecret") == []


class TestScanModes:
    """Tests for mode-aware scan() behavior."""

    def test_scan_off_returns_empty(self):
        """mode='off' should never detect anything."""
        key = "AKIA" + "IOSFODNN7EXAMPLE"
        from hooks.secrets import scan

        assert scan(f"key = {key}", mode="off") == []

    def test_scan_warn_detects_standard(self):
        """mode='warn' uses standard patterns."""
        key = "AKIA" + "IOSFODNN7EXAMPLE"
        from hooks.secrets import scan

        hits = scan(f"key = {key}", mode="warn")
        assert "aws_access_key" in hits

    def test_scan_standard_detects_standard(self):
        """mode='standard' uses standard patterns."""
        key = "AKIA" + "IOSFODNN7EXAMPLE"
        from hooks.secrets import scan

        hits = scan(f"key = {key}", mode="standard")
        assert "aws_access_key" in hits

    def test_scan_strict_detects_standard(self):
        """mode='strict' also detects standard patterns."""
        key = "AKIA" + "IOSFODNN7EXAMPLE"
        from hooks.secrets import scan

        hits = scan(f"key = {key}", mode="strict")
        assert "aws_access_key" in hits

    def test_scan_strict_detects_slack_token(self):
        """mode='strict' catches Slack tokens."""
        token = "xoxb-" + "1234567890-abcdef"
        from hooks.secrets import scan

        hits = scan(f"SLACK_TOKEN={token}", mode="strict")
        assert "slack_token" in hits

    def test_scan_standard_misses_slack_token(self):
        """mode='standard' does NOT catch Slack tokens."""
        token = "xoxb-" + "1234567890-abcdef"
        from hooks.secrets import scan

        hits = scan(f"SLACK_TOKEN={token}", mode="standard")
        assert "slack_token" not in hits

    def test_scan_strict_detects_stripe_key(self):
        """mode='strict' catches Stripe keys."""
        key = "sk_live_" + "A" * 24
        from hooks.secrets import scan

        hits = scan(f"stripe_key={key}", mode="strict")
        assert "stripe_key" in hits

    def test_scan_standard_misses_stripe_key(self):
        """mode='standard' does NOT catch Stripe keys."""
        key = "sk_live_" + "A" * 24
        from hooks.secrets import scan

        hits = scan(f"stripe_key={key}", mode="standard")
        assert "stripe_key" not in hits

    def test_scan_strict_detects_jwt(self):
        """mode='strict' catches JWT tokens."""
        # Build a realistic JWT-shaped string
        header = "eyJhbGciOiJIUzI1NiJ9"  # nosecret
        payload = "eyJzdWIiOiIxMjM0NTY3ODkwIn0"  # nosecret
        sig = "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"  # nosecret
        jwt = f"{header}.{payload}.{sig}"
        from hooks.secrets import scan

        hits = scan(f"token={jwt}", mode="strict")
        assert "jwt_token" in hits

    def test_scan_standard_misses_jwt(self):
        """mode='standard' does NOT catch JWT tokens."""
        header = "eyJhbGciOiJIUzI1NiJ9"  # nosecret
        payload = "eyJzdWIiOiIxMjM0NTY3ODkwIn0"  # nosecret
        sig = "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"  # nosecret
        jwt = f"{header}.{payload}.{sig}"
        from hooks.secrets import scan

        hits = scan(f"token={jwt}", mode="standard")
        assert "jwt_token" not in hits


class TestRedactModes:
    """Tests for mode-aware redact() behavior."""

    def test_redact_off_returns_unchanged(self):
        key = "AKIA" + "IOSFODNN7EXAMPLE"
        text = f"key: {key}"
        from hooks.secrets import redact

        assert redact(text, mode="off") == text

    def test_redact_strict_redacts_stripe(self):
        key = "sk_live_" + "A" * 24
        from hooks.secrets import redact

        result = redact(f"key={key}", mode="strict")
        assert "[REDACTED:stripe_key]" in result
        assert key not in result


class TestRedact:
    """Tests for secrets.redact()."""

    def test_redact_replaces_secrets(self):
        key = "AKIA" + "IOSFODNN7EXAMPLE"
        from hooks.secrets import redact

        result = redact(f"key: {key} rest")
        assert "[REDACTED:aws_access_key]" in result
        assert key not in result

    def test_redact_clean_text_unchanged(self):
        from hooks.secrets import redact

        text = "ls -la /tmp && echo hello"
        result = redact(text)
        assert result == text

    def test_redact_multiple_secrets(self):
        key = "AKIA" + "IOSFODNN7EXAMPLE"
        key_name = "aws_secret" + "_access_key"
        key_val = "wJalrXUtnFEMI" + "/K7MDENG/bPxRfiCYEXAMPLEKEY"
        from hooks.secrets import redact

        result = redact(f"{key} and {key_name}={key_val}")
        assert "[REDACTED:aws_access_key]" in result
        assert "[REDACTED:aws_secret_key]" in result

    def test_redact_db_url(self):
        scheme = "postgres"
        userpass = "admin:s3cr3tpassword"
        host = "db.example.com/mydb"
        url = f"{scheme}://{userpass}@{host}"
        from hooks.secrets import redact

        result = redact(url)
        assert "[REDACTED:db_url_creds]" in result
        assert "s3cr3tpassword" not in result


class TestGenericSecretShape:
    """generic_secret flags literal values, not code that names a secret."""

    @pytest.mark.parametrize(
        "text",
        [
            'typeof tokens.{k} === "string" ? tokens.{k} : tokens.refreshToken',
            'token_validity_units {{\n  {k}  = "minutes"\n  id_token = "minutes"\n}}',
            "self._{s} = settings.bridge_{s}_value",
            '{p} = os.environ["IBKR_PASSWORD"]',
            '{a} = get_secret("x")',
            "{a}: Optional[str] = None",
            "{p}=${{DB_PASSWORD}}",
        ],
    )
    def test_code_is_not_flagged(self, text):
        from hooks.secrets import scan

        filled = text.format(k="access" + "_token", s="sec" + "ret", p="pass" + "word", a="api" + "_key")
        assert "generic_secret" not in scan(filled, mode="standard")

    @pytest.mark.parametrize(
        "sep, value",
        [
            ("=", "sk9f8a7s6d5f4g3h2j1k"),
            (" = ", '"hunter2hunter2"'),
            ("=", "supersecretpassword"),
            (": ", "7fJk29sLq0pXv8Rt"),
        ],
    )
    def test_literal_is_flagged(self, sep, value):
        from hooks.secrets import scan

        assert "generic_secret" in scan("pass" + "word" + sep + value, mode="standard")

    def test_redact_leaves_code_expressions(self):
        from hooks.secrets import redact

        code = "x = settings.bridge_" + "sec" + "ret_value"
        literal = "API" + "_KEY=" + "sk9f8a7s6d5f4g3h2j1k"
        result = redact(f"{code}\n{literal}", mode="standard")
        assert code in result
        assert "[REDACTED:generic_secret]" in result
        assert "sk9f8a7s6d5f4g3h2j1k" not in result
