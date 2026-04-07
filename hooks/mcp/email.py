"""Email MCP tools."""

import json

from hooks.common import log


def register(mcp):
    @mcp.tool()
    def email_send(
        to: str,
        subject: str,
        body: str = "",
        html: str = "",
        markdown: str = "",
        title: str = "",
        template: str = "",
    ) -> str:
        """Send an email with flexible content options.

        Provide exactly one of: body (plain text), html, or markdown.
        Optionally provide an HTML template with {{placeholders}}.

        Args:
            to: Recipient(s) - comma or semicolon separated
            subject: Email subject line
            body: Plain text content
            html: HTML content
            markdown: Markdown content (will be converted to HTML)
            title: Optional title for HTML wrapper
            template: Optional HTML template with {{content}}, {{subject}}, {{timestamp}} placeholders

        Returns:
            JSON with success status and recipients_count
        """
        try:
            from hooks.integrations.mailer import EmailConfig, send_email, send_from_config

            if template:
                recipient_list = [r.strip() for r in to.replace(";", ",").split(",") if r.strip()]
                content = markdown if markdown else body if body else html

                config = EmailConfig(
                    recipients=recipient_list,
                    subject=subject,
                    content=content,
                )

                result = send_from_config(config=config, template=template)
            else:
                result = send_email(
                    to=to,
                    subject=subject,
                    body=body if body else None,
                    html=html if html else None,
                    markdown=markdown if markdown else None,
                    title=title if title else None,
                )

            return json.dumps(
                {
                    "success": result.success,
                    "recipients_count": result.recipients_count,
                    "error": result.error,
                }
            )

        except Exception as e:
            log("MCP email_send failed", {"to": to, "subject": subject, "error": str(e)})
            return json.dumps({"success": False, "error": str(e)})
