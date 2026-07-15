# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in agentihooks, please report it responsibly.

**Do NOT open a public GitHub issue for security vulnerabilities.**

Instead, email **security@thecloudclockwork.com** with:

- Description of the vulnerability
- Steps to reproduce
- Potential impact
- Suggested fix (if any)

We will acknowledge receipt within 48 hours and aim to provide a fix or mitigation within 7 days for critical issues.

## Security Considerations

agentihooks runs as a hook system and MCP server inside Claude Code agent containers. Keep in mind:

- **MCP tools execute with the agent's permissions** -- tools like `channel_publish` operate under the agent's credentials
- **Integration credentials** (GitHub App, AWS, SMTP, etc.) are read from environment variables, never hardcoded
- **No telemetry or phone-home code** exists in this project
- **Redis connections** are optional and default to localhost

## Best Practices When Forking

- Never commit `.env` files or credentials (`.gitignore` covers this)
- Review `profiles/*/settings.overrides.json` before pushing if you add custom permissions
- Use `ALLOWED_TOOLS` or `MCP_CATEGORIES` to restrict tool exposure in sensitive environments
- Run the MCP server on localhost only; do not expose it to the network

## Supported Versions

| Version | Supported |
|---------|-----------|
| 0.1.x   | Yes       |
