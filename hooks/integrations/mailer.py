#!/usr/bin/env python3
"""Email notification module for hooks.

Send HTML email notifications via SMTP with markdown conversion support.

Environment Variables:
    SMTP_SERVER: SMTP server hostname (required)
    SMTP_PORT: SMTP port (default: 25 for relay, 587 for auth)
    SENDER_EMAIL: Default sender email address (required)
    SMTP_SERVER_IP: Optional fallback IP address for SMTP server
    SMTP_USER: SMTP username for authentication (optional)
    SMTP_PASS: SMTP password or app token for authentication (optional)

Note: If SMTP_USER and SMTP_PASS are provided, STARTTLS authentication is used.
If not provided, assumes open SMTP relay (no authentication)

Usage:
    from hooks.integrations.mailer import EmailClient, send_email

    # Quick send
    send_email(
        to="user@example.com,team@example.com",
        subject="Notification",
        body="# Report\n\nMarkdown content here...",
    )

    # Using client
    client = EmailClient.get_client()
    client.send_html(
        recipients=["user@example.com"],
        subject="Report",
        html_content="<h1>HTML content</h1>",
    )
"""

import json
import os
import re
import smtplib
import socket
import sys
from dataclasses import dataclass
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import List, Optional, Union

# Add parent directories to path for direct script execution
_script_dir = Path(__file__).resolve().parent
_app_dir = _script_dir.parent.parent  # /app
if str(_app_dir) not in sys.path:
    sys.path.insert(0, str(_app_dir))

from hooks.common import log
from hooks.integrations.base import IntegrationBase, IntegrationRegistry

# =============================================================================
# INTEGRATION DEFINITION
# =============================================================================


@IntegrationRegistry.register
class EmailIntegration(IntegrationBase):
    """Email/SMTP integration configuration checker."""

    INTEGRATION_NAME = "email"
    REQUIRED_ENV_VARS = {
        "SMTP_SERVER": "SMTP server hostname for sending emails",
        "SENDER_EMAIL": "Default sender email address",
    }
    OPTIONAL_ENV_VARS = {
        "SMTP_PORT": "SMTP port (default: 25 for relay, 587 for auth)",
        "SMTP_SERVER_IP": "Fallback IP address for SMTP server",
        "SMTP_USER": "SMTP username for authentication",
        "SMTP_PASS": "SMTP password or app token for authentication",
    }


# =============================================================================
# DATA CLASSES
# =============================================================================


@dataclass
class EmailResult:
    """Result of email send operation."""

    success: bool
    recipients_count: int
    error: Optional[str] = None


# =============================================================================
# EMAIL CLIENT
# =============================================================================


class EmailClient:
    """SMTP email client with HTML and markdown support."""

    _instance: Optional["EmailClient"] = None

    def __init__(
        self,
        smtp_server: Optional[str] = None,
        smtp_port: Optional[int] = None,
        sender: Optional[str] = None,
    ):
        """Initialize email client.

        Args:
            smtp_server: SMTP server hostname (default: from env SMTP_SERVER)
            smtp_port: SMTP port (default: from env SMTP_PORT or 25)
            sender: Default sender email address (default: from env SENDER_EMAIL)
        """
        self._smtp_server = smtp_server or os.getenv("SMTP_SERVER", "")
        self._sender = sender or os.getenv("SENDER_EMAIL", "")
        self._smtp_server_ip = os.getenv("SMTP_SERVER_IP", "")  # Optional fallback IP

        # SMTP Authentication (optional)
        self._smtp_user = os.getenv("SMTP_USER", "")
        self._smtp_pass = os.getenv("SMTP_PASS", "")

        # Determine if authentication is required
        self._use_auth = bool(self._smtp_user and self._smtp_pass)

        # Adjust default port based on auth mode
        if self._use_auth and smtp_port is None and "SMTP_PORT" not in os.environ:
            self._smtp_port = 587  # Default to STARTTLS port for authenticated connections
        else:
            self._smtp_port = smtp_port or int(os.getenv("SMTP_PORT", "25"))

    @classmethod
    def get_client(
        cls,
        smtp_server: Optional[str] = None,
        smtp_port: Optional[int] = None,
        sender: Optional[str] = None,
    ) -> "EmailClient":
        """
        Get singleton email client instance.

        Args:
            smtp_server: Optional SMTP server override.
            smtp_port: Optional SMTP port override.
            sender: Optional sender email override.

        Returns:
            EmailClient instance.
        """
        if cls._instance is None or any([smtp_server, smtp_port, sender]):
            cls._instance = cls(smtp_server, smtp_port, sender)
        return cls._instance

    @classmethod
    def clear_cache(cls) -> None:
        """Clear the cached client instance."""
        cls._instance = None

    def should_skip(self) -> bool:
        """Check if email sending should be skipped.

        Returns:
            True if SMTP server or sender email not configured
        """
        if not self._smtp_server:
            log(
                "Email send skipped: SMTP_SERVER not configured",
                {"integration": "email", "reason": "missing_config", "var": "SMTP_SERVER"},
            )
            return True

        if not self._sender:
            log(
                "Email send skipped: SENDER_EMAIL not configured",
                {"integration": "email", "reason": "missing_config", "var": "SENDER_EMAIL"},
            )
            return True

        # Validate auth credentials consistency
        if bool(self._smtp_user) != bool(self._smtp_pass):
            log(
                "Email send skipped: SMTP_USER and SMTP_PASS must both be set or both empty",
                {"integration": "email", "reason": "incomplete_auth_config"},
            )
            return True

        return False

    def reachable(self, timeout: float = 3.0) -> bool:
        """True when the relay name, else its fallback address, accepts a TCP connection."""
        hosts = [self._smtp_server, self._smtp_server_ip] if self._smtp_server else []
        for host in filter(None, hosts):
            try:
                socket.create_connection((host, self._smtp_port), timeout=timeout).close()
            except OSError:
                continue
            return True
        return False

    def _authenticate_smtp(self, server: smtplib.SMTP) -> None:
        """Authenticate SMTP connection with STARTTLS if credentials are configured.

        Args:
            server: Active SMTP connection

        Raises:
            smtplib.SMTPAuthenticationError: If authentication fails
        """
        if not self._use_auth:
            return

        try:
            # Enable TLS encryption
            server.starttls()
            log("STARTTLS enabled for SMTP connection")

            # Authenticate with credentials
            server.login(self._smtp_user, self._smtp_pass)
            log(f"SMTP authentication successful for user: {self._smtp_user}")

        except smtplib.SMTPAuthenticationError as e:
            log("SMTP authentication failed", {"user": self._smtp_user, "error": str(e)})
            raise
        except smtplib.SMTPException as e:
            log("SMTP TLS/auth error", {"error": str(e)})
            raise

    def send_html(
        self,
        recipients: List[str],
        subject: str,
        html_content: str,
        sender: Optional[str] = None,
    ) -> EmailResult:
        """Send HTML email.

        Args:
            recipients: List of recipient email addresses.
            subject: Email subject line.
            html_content: HTML content (complete document or body fragment).
            sender: Optional sender override.

        Returns:
            EmailResult with success status.
        """
        # Check if email is configured
        if self.should_skip():
            return EmailResult(
                success=True,
                recipients_count=0,
                error="Email not configured (SMTP_SERVER or SENDER_EMAIL not set)",
            )

        if not recipients:
            return EmailResult(success=False, recipients_count=0, error="No recipients specified")

        # Filter empty recipients
        recipients = [r.strip() for r in recipients if r.strip()]
        if not recipients:
            return EmailResult(success=False, recipients_count=0, error="All recipient addresses are empty")

        from_addr = sender or self._sender

        log(
            "Sending email",
            {
                "smtp_server": self._smtp_server,
                "port": self._smtp_port,
                "from": from_addr,
                "to": recipients,
                "subject": subject,
            },
        )

        try:
            msg = MIMEMultipart("alternative")
            msg["From"] = from_addr
            msg["To"] = ", ".join(recipients)
            msg["Subject"] = subject

            # Attach HTML part
            html_part = MIMEText(html_content, "html", "utf-8")
            msg.attach(html_part)

            # Try primary server
            try:
                with smtplib.SMTP(self._smtp_server, self._smtp_port, timeout=30) as server:
                    # Authenticate if credentials are configured
                    self._authenticate_smtp(server)

                    server.send_message(msg)
                log(f"Email sent successfully to {len(recipients)} recipient(s)")
                return EmailResult(success=True, recipients_count=len(recipients))

            except (smtplib.SMTPException, OSError) as e:
                # Try IP fallback if configured
                if self._smtp_server_ip and self._smtp_server != self._smtp_server_ip:
                    log(f"Primary SMTP failed ({e}), trying IP fallback: {self._smtp_server_ip}")
                    with smtplib.SMTP(self._smtp_server_ip, self._smtp_port, timeout=30) as server:
                        # Authenticate fallback connection
                        self._authenticate_smtp(server)

                        server.send_message(msg)
                    log(f"Email sent via fallback IP to {len(recipients)} recipient(s)")
                    return EmailResult(success=True, recipients_count=len(recipients))
                raise

        except smtplib.SMTPAuthenticationError as e:
            error = f"SMTP authentication failed: {e}"
            log(error, {"smtp_server": self._smtp_server, "smtp_user": self._smtp_user})
            return EmailResult(success=False, recipients_count=len(recipients), error=error)

        except smtplib.SMTPRecipientsRefused as e:
            error = f"Recipients refused: {e}"
            log(error)
            return EmailResult(success=False, recipients_count=0, error=error)

        except smtplib.SMTPException as e:
            error = f"SMTP error: {e}"
            log(error)
            return EmailResult(success=False, recipients_count=0, error=error)

        except Exception as e:
            error = f"Failed to send email: {e}"
            log(error)
            return EmailResult(success=False, recipients_count=0, error=error)

    def send_markdown(
        self,
        recipients: List[str],
        subject: str,
        markdown_content: str,
        title: Optional[str] = None,
        sender: Optional[str] = None,
    ) -> EmailResult:
        """
        Send email with markdown content converted to HTML.

        Args:
            recipients: List of recipient email addresses.
            subject: Email subject line.
            markdown_content: Markdown content to convert.
            title: Optional title for HTML wrapper.
            sender: Optional sender override.

        Returns:
            EmailResult with success status.
        """
        html_body = markdown_to_html(markdown_content)
        html_content = wrap_html_body(html_body, title or subject)
        return self.send_html(recipients, subject, html_content, sender)

    def send_text(
        self,
        recipients: List[str],
        subject: str,
        text_content: str,
        title: Optional[str] = None,
        sender: Optional[str] = None,
    ) -> EmailResult:
        """
        Send email with plain text wrapped in HTML.

        Args:
            recipients: List of recipient email addresses.
            subject: Email subject line.
            text_content: Plain text content.
            title: Optional title for HTML wrapper.
            sender: Optional sender override.

        Returns:
            EmailResult with success status.
        """
        # Convert newlines to paragraphs
        paragraphs = text_content.strip().split("\n\n")
        html_body = "".join(f"<p>{p.replace(chr(10), '<br>')}</p>" for p in paragraphs)
        html_content = wrap_html_body(html_body, title or subject)
        return self.send_html(recipients, subject, html_content, sender)

    @property
    def smtp_server(self) -> str:
        """Get configured SMTP server."""
        return self._smtp_server

    @property
    def smtp_port(self) -> int:
        """Get configured SMTP port."""
        return self._smtp_port

    @property
    def sender(self) -> str:
        """Get configured sender address."""
        return self._sender


# =============================================================================
# MARKDOWN TO HTML CONVERSION
# =============================================================================


def markdown_to_html(content: str) -> str:
    """
    Convert markdown to basic HTML.

    Supports: headers, bold, italic, code blocks, inline code,
    tables, horizontal rules, lists, and checkboxes.

    Args:
        content: Markdown content.

    Returns:
        HTML content.
    """
    html = content

    # Escape HTML entities first
    html = html.replace("&", "&amp;")
    html = html.replace("<", "&lt;")
    html = html.replace(">", "&gt;")

    # Headers
    html = re.sub(r"^#### (.+)$", r"<h4>\1</h4>", html, flags=re.MULTILINE)
    html = re.sub(r"^### (.+)$", r"<h3>\1</h3>", html, flags=re.MULTILINE)
    html = re.sub(r"^## (.+)$", r"<h2>\1</h2>", html, flags=re.MULTILINE)
    html = re.sub(r"^# (.+)$", r"<h1>\1</h1>", html, flags=re.MULTILINE)

    # Bold and italic
    html = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", html)
    html = re.sub(r"\*(.+?)\*", r"<em>\1</em>", html)

    # Code blocks
    def restore_code_block(match):
        code = match.group(2)
        code = code.replace("&lt;", "<").replace("&gt;", ">")
        return (
            f'<pre style="background:#f4f4f4;padding:10px;border-radius:4px;overflow-x:auto;"><code>{code}</code></pre>'
        )

    html = re.sub(r"```(\w*)\n(.*?)```", restore_code_block, html, flags=re.DOTALL)

    # Inline code
    html = re.sub(
        r"`(.+?)`",
        r'<code style="background:#f4f4f4;padding:2px 4px;border-radius:2px;">\1</code>',
        html,
    )

    # Tables
    def convert_table(match):
        lines = match.group(0).strip().split("\n")
        if len(lines) < 2:
            return match.group(0)

        table_html = '<table style="border-collapse:collapse;width:100%;margin:10px 0;">'
        for i, line in enumerate(lines):
            if "---" in line:
                continue
            cells = [c.strip() for c in line.strip("|").split("|")]
            tag = "th" if i == 0 else "td"
            style = "border:1px solid #ddd;padding:8px;text-align:left;"
            if i == 0:
                style += "background:#f2f2f2;font-weight:bold;"
            row = "".join(f'<{tag} style="{style}">{c}</{tag}>' for c in cells)
            table_html += f"<tr>{row}</tr>"
        table_html += "</table>"
        return table_html

    html = re.sub(r"(\|.+\|\n)+", convert_table, html)

    # Horizontal rules
    html = re.sub(
        r"^---+$",
        '<hr style="border:none;border-top:1px solid #ddd;margin:20px 0;">',
        html,
        flags=re.MULTILINE,
    )

    # Lists
    html = re.sub(r"^- (.+)$", r"<li>\1</li>", html, flags=re.MULTILINE)
    html = re.sub(
        r"(<li>.*</li>\n?)+",
        r'<ul style="margin:10px 0;padding-left:20px;">\g<0></ul>',
        html,
    )

    # Checkboxes
    html = html.replace("[ ]", "&#9744;")
    html = html.replace("[x]", "&#9745;")

    # Paragraphs
    lines = html.split("\n")
    result = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("<") and not stripped.endswith(">"):
            result.append(f'<p style="margin:10px 0;">{stripped}</p>')
        else:
            result.append(line)
    html = "\n".join(result)

    return html


def wrap_html_body(content: str, title: str = "Notification") -> str:
    """
    Wrap content in a complete HTML document with styling.

    Args:
        content: HTML content to wrap.
        title: Document title.

    Returns:
        Complete HTML document.
    """
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    return f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>{title}</title>
</head>
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif; line-height: 1.6; color: #333; max-width: 800px; margin: 0 auto; padding: 20px;">
    <div style="background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 20px; border-radius: 8px 8px 0 0;">
        <h1 style="margin: 0; font-size: 24px;">{title}</h1>
        <p style="margin: 5px 0 0 0; opacity: 0.9; font-size: 14px;">Generated: {timestamp}</p>
    </div>
    <div style="background: #fff; border: 1px solid #ddd; border-top: none; padding: 20px; border-radius: 0 0 8px 8px;">
        {content}
    </div>
    <div style="text-align: center; padding: 20px; color: #666; font-size: 12px;">
        <p>Automated notification</p>
    </div>
</body>
</html>"""


# =============================================================================
# UTILITY FUNCTIONS
# =============================================================================


def parse_recipients(recipients_str: str) -> List[str]:
    """
    Parse comma or semicolon separated email list.

    Args:
        recipients_str: String with email addresses.

    Returns:
        List of email addresses.
    """
    # Support both comma and semicolon separators
    recipients = re.split(r"[,;]\s*", recipients_str)
    return [r.strip() for r in recipients if r.strip()]


# =============================================================================
# CONFIG FILE LOADING
# =============================================================================


@dataclass
class EmailRecipientCategories:
    """Recipient categories for different notification types."""

    error_recipients: List[str]
    success_recipients: List[str]
    notifications_recipients: List[str]

    def get_recipients(self, category: str) -> List[str]:
        """Get recipients for specified category."""
        return getattr(self, category, [])


@dataclass
class EmailConfig:
    """Email configuration from JSON file (V2 format)."""

    version: str
    categories: EmailRecipientCategories
    recipients: List[str]  # Fallback recipients
    defaults: dict  # {subject: str, content: str}

    @classmethod
    def from_dict(cls, data: dict) -> "EmailConfig":
        """Create config from dictionary.

        Args:
            data: Dictionary with V2 format (version, categories, recipients, defaults)

        Returns:
            EmailConfig instance

        Raises:
            ValueError: If required fields missing or invalid version
        """
        # Validate version field
        version = data.get("version")
        if not version:
            raise ValueError(
                "Missing 'version' field. This appears to be V1 format. "
                "Please migrate to V2 format with recipient categories. "
                'V2 format requires: {"version": "2.0", "categories": {...}, "recipients": [...], "defaults": {...}}'
            )
        if version != "2.0":
            raise ValueError(f"Unsupported version: {version}. Expected '2.0'")

        # Parse categories
        categories_data = data.get("categories", {})
        categories = EmailRecipientCategories(
            error_recipients=categories_data.get("error_recipients", []),
            success_recipients=categories_data.get("success_recipients", []),
            notifications_recipients=categories_data.get("notifications_recipients", []),
        )

        # Parse fallback recipients
        recipients = data.get("recipients", [])

        # Parse defaults (now optional)
        defaults = data.get("defaults", {})

        return cls(version=version, categories=categories, recipients=recipients, defaults=defaults)

    def get_recipients_for_category(self, category: str) -> List[str]:
        """Get recipients for category with fallback to flat list.

        Args:
            category: Category name (e.g., "error_recipients")

        Returns:
            List of email addresses for the category, or fallback recipients if category is empty
        """
        category_recipients = self.categories.get_recipients(category)
        if category_recipients:
            return category_recipients
        return self.recipients  # Fallback


def load_email_config(config_path: Path) -> Optional[EmailConfig]:
    """Load email configuration from JSON file.

    Args:
        config_path: Path to email.json file

    Returns:
        EmailConfig instance, or None if file not found/invalid
    """
    try:
        if not config_path.exists():
            log("Email config not found", {"path": str(config_path)})
            return None

        data = json.loads(config_path.read_text(encoding="utf-8"))
        config = EmailConfig.from_dict(data)

        log(
            "Loaded email config",
            {
                "path": str(config_path),
                "recipients_count": len(config.recipients),
                "version": config.version,
            },
        )

        return config

    except json.JSONDecodeError as e:
        log("Invalid JSON in email config", {"path": str(config_path), "error": str(e)})
        return None
    except ValueError as e:
        log("Invalid email config", {"path": str(config_path), "error": str(e)})
        return None
    except Exception as e:
        log("Failed to load email config", {"path": str(config_path), "error": str(e)})
        return None


def load_html_template(template_path: Path) -> Optional[str]:
    """Load HTML template from file.

    Args:
        template_path: Path to template.html file

    Returns:
        Template HTML string, or None if file not found
    """
    try:
        if not template_path.exists():
            log("HTML template not found", {"path": str(template_path)})
            return None

        template = template_path.read_text(encoding="utf-8")

        log(
            "Loaded HTML template",
            {
                "path": str(template_path),
                "size": len(template),
            },
        )

        return template

    except Exception as e:
        log("Failed to load HTML template", {"path": str(template_path), "error": str(e)})
        return None


def inject_template_variables(template: str, context: dict) -> str:
    """Inject variables into HTML template.

    Replaces placeholders like {{variable}} with values from context.

    Args:
        template: HTML template with {{variable}} placeholders
        context: Dictionary with variable values

    Returns:
        HTML with placeholders replaced
    """
    html = template

    for key, value in context.items():
        placeholder = f"{{{{{key}}}}}"  # {{key}}
        html = html.replace(placeholder, str(value))

    # Warn about unreplaced placeholders
    remaining = re.findall(r"\{\{(\w+)\}\}", html)
    if remaining:
        log("Unreplaced template placeholders", {"placeholders": remaining})

    return html


def scan_for_config_files(working_dir: Optional[Path] = None) -> tuple[Optional[EmailConfig], Optional[str]]:
    """Scan directory for email.json and template.html files.

    Args:
        working_dir: Directory to scan (default: current working directory)

    Returns:
        Tuple of (EmailConfig or None, template_html or None)
    """
    directory = working_dir or Path.cwd()

    log("Scanning for email config files", {"directory": str(directory)})

    # Look for email.json
    config_path = directory / "email.json"
    config = load_email_config(config_path)

    # Look for template.html
    template_path = directory / "template.html"
    template = load_html_template(template_path)

    return config, template


# =============================================================================
# TEMPLATE-BASED SENDING
# =============================================================================


def send_from_config(
    config: EmailConfig,
    template: Optional[str] = None,
    sender: Optional[str] = None,
) -> EmailResult:
    """Send email using config and optional template (V2 format).

    Args:
        config: EmailConfig V2 with defaults dict
        template: Optional HTML template with {{placeholders}}
        sender: Optional sender override

    Returns:
        EmailResult with success status
    """
    try:
        # Get subject and content from defaults
        subject = config.defaults.get("subject", "Notification")
        content = config.defaults.get("content", "")

        # Convert content (assume markdown)
        html_content = markdown_to_html(content)

        # Build template context
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        context = {
            "content": html_content,
            "subject": subject,
            "timestamp": timestamp,
            "recipients": ", ".join(config.recipients),
        }

        # Use template or default HTML wrapper
        if template:
            log("Using custom HTML template")
            final_html = inject_template_variables(template, context)
        else:
            log("Using default HTML wrapper")
            final_html = wrap_html_body(html_content, subject)

        # Send email
        client = EmailClient.get_client()
        return client.send_html(
            recipients=config.recipients,
            subject=subject,
            html_content=final_html,
            sender=sender,
        )

    except Exception as e:
        error_msg = f"Failed to send from config: {e}"
        log(error_msg)
        return EmailResult(success=False, recipients_count=0, error=error_msg)


def send_from_config_with_category(
    config: EmailConfig,
    category: str,
    template: Optional[str] = None,
    content_override: Optional[str] = None,
    subject_override: Optional[str] = None,
    extra_context: Optional[dict] = None,
    sender: Optional[str] = None,
) -> EmailResult:
    """Send email using config with category selection and custom content.

    Args:
        config: EmailConfig V2 with recipient categories
        category: Category name (e.g., "error_recipients")
        template: Optional HTML template with {{placeholders}}
        content_override: Optional markdown content (overrides config.defaults.content)
        subject_override: Optional subject (overrides config.defaults.subject)
        extra_context: Optional dict of additional template variables
        sender: Optional sender override

    Returns:
        EmailResult with success status
    """
    try:
        # Get recipients for category
        recipients = config.get_recipients_for_category(category)
        if not recipients:
            return EmailResult(
                success=False, recipients_count=0, error=f"No recipients configured for category: {category}"
            )

        # Determine content and subject
        content = content_override or config.defaults.get("content", "")
        subject = subject_override or config.defaults.get("subject", "Notification")

        # Convert markdown to HTML
        html_content = markdown_to_html(content)

        # Build template context
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        context = {
            "content": html_content,
            "subject": subject,
            "timestamp": timestamp,
            "recipients": ", ".join(recipients),
            "category": category,
            "alert_level": _infer_alert_level(category),
        }

        # Merge extra context
        if extra_context:
            context.update(extra_context)

        # Use template or default wrapper
        if template:
            log("Using custom HTML template")
            final_html = inject_template_variables(template, context)
        else:
            log("Using default HTML wrapper")
            final_html = wrap_html_body(html_content, subject)

        # Send email
        client = EmailClient.get_client()
        return client.send_html(
            recipients=recipients,
            subject=subject,
            html_content=final_html,
            sender=sender,
        )

    except Exception as e:
        error_msg = f"Failed to send from config with category: {e}"
        log(error_msg)
        return EmailResult(success=False, recipients_count=0, error=error_msg)


def _infer_alert_level(category: str) -> str:
    """Infer alert level from category name.

    Args:
        category: Category name (e.g., "error_recipients")

    Returns:
        Alert level string (ERROR, SUCCESS, INFO, WARNING)
    """
    if "error" in category:
        return "ERROR"
    elif "success" in category:
        return "SUCCESS"
    elif "notification" in category:
        return "INFO"
    return "INFO"


def send_error_notification(
    config: EmailConfig,
    template: Optional[str] = None,
    content: str = "",
    subject: str = "",
    **extra_context,
) -> EmailResult:
    """Helper to send error notification to error_recipients.

    Args:
        config: EmailConfig V2 with recipient categories
        template: Optional HTML template
        content: Markdown content for notification
        subject: Email subject
        **extra_context: Additional template variables

    Returns:
        EmailResult with success status
    """
    return send_from_config_with_category(
        config=config,
        category="error_recipients",
        template=template,
        content_override=content,
        subject_override=subject,
        extra_context=extra_context,
    )


def send_success_notification(
    config: EmailConfig,
    template: Optional[str] = None,
    content: str = "",
    subject: str = "",
    **extra_context,
) -> EmailResult:
    """Helper to send success notification to success_recipients.

    Args:
        config: EmailConfig V2 with recipient categories
        template: Optional HTML template
        content: Markdown content for notification
        subject: Email subject
        **extra_context: Additional template variables

    Returns:
        EmailResult with success status
    """
    return send_from_config_with_category(
        config=config,
        category="success_recipients",
        template=template,
        content_override=content,
        subject_override=subject,
        extra_context=extra_context,
    )


def send_info_notification(
    config: EmailConfig,
    template: Optional[str] = None,
    content: str = "",
    subject: str = "",
    **extra_context,
) -> EmailResult:
    """Helper to send info notification to notifications_recipients.

    Args:
        config: EmailConfig V2 with recipient categories
        template: Optional HTML template
        content: Markdown content for notification
        subject: Email subject
        **extra_context: Additional template variables

    Returns:
        EmailResult with success status
    """
    return send_from_config_with_category(
        config=config,
        category="notifications_recipients",
        template=template,
        content_override=content,
        subject_override=subject,
        extra_context=extra_context,
    )


# =============================================================================
# CONVENIENCE FUNCTIONS
# =============================================================================


def send_email(
    to: Union[str, List[str]],
    subject: str,
    body: Optional[str] = None,
    html: Optional[str] = None,
    markdown: Optional[str] = None,
    title: Optional[str] = None,
    sender: Optional[str] = None,
) -> EmailResult:
    """
    Send email with flexible content options.

    Provide exactly one of: body (plain text), html, or markdown.

    Args:
        to: Recipient(s) - string (comma/semicolon separated) or list.
        subject: Email subject line.
        body: Plain text content.
        html: HTML content.
        markdown: Markdown content (will be converted to HTML).
        title: Optional title for HTML wrapper.
        sender: Optional sender override.

    Returns:
        EmailResult with success status.

    Examples:
        # Plain text
        send_email("user@example.com", "Hello", body="Plain text message")

        # Markdown
        send_email(
            "user@example.com,team@example.com",
            "Report",
            markdown="# Report\\n\\n**Bold** text",
        )

        # HTML
        send_email(
            ["user@example.com"],
            "Alert",
            html="<h1>Alert</h1><p>Something happened</p>",
        )
    """
    # Parse recipients
    if isinstance(to, str):
        recipients = parse_recipients(to)
    else:
        recipients = to

    client = EmailClient.get_client()

    if html:
        # If HTML doesn't look complete, wrap it
        if not html.strip().lower().startswith("<!doctype") and not html.strip().lower().startswith("<html"):
            html = wrap_html_body(html, title or subject)
        return client.send_html(recipients, subject, html, sender)

    if markdown:
        return client.send_markdown(recipients, subject, markdown, title, sender)

    if body:
        return client.send_text(recipients, subject, body, title, sender)

    return EmailResult(success=False, recipients_count=0, error="No content provided (body, html, or markdown)")


def send_markdown_file(
    to: Union[str, List[str]],
    subject: str,
    file_path: Union[str, Path],
    title: Optional[str] = None,
    sender: Optional[str] = None,
) -> EmailResult:
    """
    Send email with markdown file content.

    Args:
        to: Recipient(s) - string (comma/semicolon separated) or list.
        subject: Email subject line.
        file_path: Path to markdown file.
        title: Optional title for HTML wrapper.
        sender: Optional sender override.

    Returns:
        EmailResult with success status.
    """
    path = Path(file_path)
    if not path.exists():
        return EmailResult(success=False, recipients_count=0, error=f"File not found: {file_path}")

    content = path.read_text(encoding="utf-8")
    return send_email(to, subject, markdown=content, title=title, sender=sender)


# =============================================================================
# CLI INTERFACE
# =============================================================================


def main():
    """CLI entry point for email operations."""
    import json as json_module  # Avoid shadowing json variable

    if len(sys.argv) < 2:
        print("Usage: /app/hooks/integrations/mailer.py <command> [args]")
        print("")
        print("Commands:")
        print("  check                    Check configuration status")
        print("  check --json             Output status as JSON")
        print("  send --recipients <emails> --subject <text> --content <text>")
        print("      Send email with CLI arguments")
        print("")
        print("  send --scan-paths [--working-dir <path>] [--demo]")
        print("      Scan for email.json and template.html in directory")
        print("      --demo: Test mode, prints message and exits")
        print("")
        print("Environment Variables:")
        print("  SMTP_SERVER           SMTP server hostname (required)")
        print("  SMTP_PORT             SMTP port (default: 25)")
        print("  SENDER_EMAIL          Sender email address (required)")
        print("  SMTP_SERVER_IP        Optional fallback IP address")
        print("  SMTP_USER             SMTP username for authentication")
        print("  SMTP_PASS             SMTP password for authentication")
        print("")
        print("Examples:")
        print("  # Check configuration")
        print("  /app/hooks/integrations/mailer.py check")
        print("")
        print("  # Send with CLI args")
        print("  /app/hooks/integrations/mailer.py send \\")
        print('    --recipients "user@example.com,team@example.com" \\')
        print('    --subject "Report" \\')
        print('    --content "# Report\\n\\nDetails here..."')
        print("")
        print("  # Send with config files (scans current directory)")
        print("  /app/hooks/integrations/mailer.py send --scan-paths")
        print("")
        print("  # Send with config files (custom directory)")
        print("  /app/hooks/integrations/mailer.py send --scan-paths --working-dir /path/to/dir")
        print("")
        print("  # Demo mode (testing)")
        print("  /app/hooks/integrations/mailer.py send --demo")
        sys.exit(1)

    command = sys.argv[1]

    if command == "check":
        # Check configuration status
        integration = EmailIntegration()
        as_json = "--json" in sys.argv
        integration.print_status(as_json=as_json)
        sys.exit(0 if integration.is_configured else 1)

    elif command == "send":
        try:
            # Check for demo flag (testing mode)
            if "--demo" in sys.argv:
                log("[DEMO] Email hook triggered - SMTP integration ready")
                sys.exit(0)

            # Parse flags
            scan_paths = False
            working_dir = None
            recipients = None
            subject = None
            content = None

            i = 2
            while i < len(sys.argv):
                if sys.argv[i] == "--scan-paths":
                    scan_paths = True
                    i += 1
                elif sys.argv[i] == "--working-dir" and i + 1 < len(sys.argv):
                    working_dir = Path(sys.argv[i + 1])
                    i += 2
                elif sys.argv[i] == "--recipients" and i + 1 < len(sys.argv):
                    recipients = sys.argv[i + 1]
                    i += 2
                elif sys.argv[i] == "--subject" and i + 1 < len(sys.argv):
                    subject = sys.argv[i + 1]
                    i += 2
                elif sys.argv[i] == "--content" and i + 1 < len(sys.argv):
                    content = sys.argv[i + 1]
                    i += 2
                elif sys.argv[i] == "--demo":
                    # Already handled above, skip
                    i += 1
                else:
                    i += 1

            # Mode 1: Scan for config files
            if scan_paths:
                log("Scanning for email config files")
                config, template = scan_for_config_files(working_dir)

                if not config:
                    print(
                        json_module.dumps(
                            {
                                "success": False,
                                "error": "email.json not found or invalid",
                            }
                        )
                    )
                    sys.exit(1)

                result = send_from_config(config, template)

                print(
                    json_module.dumps(
                        {
                            "success": result.success,
                            "recipients_count": result.recipients_count,
                            "error": result.error,
                        }
                    )
                )

                sys.exit(0 if result.success else 1)

            # Mode 2: CLI arguments
            else:
                if not recipients or not subject or not content:
                    print(
                        json_module.dumps(
                            {
                                "success": False,
                                "error": "--recipients, --subject, and --content are required",
                            }
                        )
                    )
                    sys.exit(1)

                # Parse recipients (comma-separated string)
                recipient_list = parse_recipients(recipients)

                # Send email using existing function
                result = send_email(
                    to=recipient_list,
                    subject=subject,
                    markdown=content,  # Treat content as markdown
                )

                print(
                    json_module.dumps(
                        {
                            "success": result.success,
                            "recipients_count": result.recipients_count,
                            "error": result.error,
                        }
                    )
                )

                sys.exit(0 if result.success else 1)

        except Exception as e:
            print(
                json_module.dumps(
                    {
                        "success": False,
                        "error": str(e),
                    }
                )
            )
            sys.exit(1)

    else:
        print(f"Error: Unknown command '{command}'")
        print("Usage: /app/hooks/integrations/mailer.py <command> [args]")
        sys.exit(1)


if __name__ == "__main__":
    main()
