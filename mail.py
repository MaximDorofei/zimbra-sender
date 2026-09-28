#!/usr/bin/env python3
from __future__ import annotations

import argparse
import configparser
import getpass
import logging
import mimetypes
import os
import smtplib
import ssl
import sys
from email.message import EmailMessage
from email.utils import formataddr, getaddresses
from pathlib import Path
from typing import List, Optional, Sequence

LOG = logging.getLogger("zimbra_mailer")

DEFAULT_CONFIG_PATHS = [
    Path.cwd() / "zimbra_mailer.ini",
    Path.home() / ".config" / "zimbra_mailer" / "config.ini",
    Path.home() / ".zimbra_mailer.ini",
]


class MailerError(Exception):
    """Base error with an associated process exit code."""

    exit_code = 1


class ConfigError(MailerError):
    exit_code = 1


class AuthError(MailerError):
    exit_code = 2


class ConnectionErrorX(MailerError):
    exit_code = 3


class SendError(MailerError):
    exit_code = 4


def load_config_file(path: Optional[str]) -> dict:
    candidates: List[Path]
    if path:
        candidates = [Path(path)]
    else:
        candidates = DEFAULT_CONFIG_PATHS

    for candidate in candidates:
        if candidate.is_file():
            parser = configparser.ConfigParser()
            try:
                parser.read(candidate, encoding="utf-8")
            except configparser.Error as exc:
                raise ConfigError(f"Could not parse config file {candidate}: {exc}") from exc
            if "smtp" in parser:
                LOG.debug("Loaded config from %s", candidate)
                return dict(parser["smtp"])
            LOG.debug("Config file %s has no [smtp] section; ignoring", candidate)
            return {}
    if path:
        raise ConfigError(f"Config file not found: {path}")
    return {}

def build_message(
    sender: str,
    sender_name: Optional[str],
    to: Sequence[str],
    cc: Sequence[str],
    bcc: Sequence[str],
    reply_to: Optional[str],
    subject: str,
    body_text: Optional[str],
    body_html: Optional[str],
    attachments: Sequence[str],
) -> EmailMessage:
    if not to and not cc and not bcc:
        raise ConfigError("At least one recipient (--to/--cc/--bcc) is required.")
    if not body_text and not body_html:
        raise ConfigError("A message body is required (--body, --body-file, or --html-body-file).")

    msg = EmailMessage()
    msg["From"] = formataddr((sender_name, sender)) if sender_name else sender
    if to:
        msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    if reply_to:
        msg["Reply-To"] = reply_to
    msg["Subject"] = subject

    if body_text and body_html:
        msg.set_content(body_text)
        msg.add_alternative(body_html, subtype="html")
    elif body_html:
        msg.set_content("This message requires an HTML-capable mail client.")
        msg.add_alternative(body_html, subtype="html")
    else:
        msg.set_content(body_text)

    for path_str in attachments:
        path = Path(path_str)
        if not path.is_file():
            raise ConfigError(f"Attachment not found: {path}")
        ctype, encoding = mimetypes.guess_type(str(path))
        if ctype is None or encoding is not None:
            ctype = "application/octet-stream"
        maintype, subtype = ctype.split("/", 1)
        data = path.read_bytes()
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=path.name)
        LOG.debug("Attached %s (%d bytes, %s)", path.name, len(data), ctype)

    return msg


def all_envelope_recipients(to: Sequence[str], cc: Sequence[str], bcc: Sequence[str]) -> List[str]:
    combined = ", ".join(a for a in (*to, *cc, *bcc) if a)
    return [addr for _, addr in getaddresses([combined]) if addr]

def send(
    host: str,
    port: int,
    username: Optional[str],
    password: Optional[str],
    use_ssl: bool,
    use_starttls: bool,
    no_auth: bool,
    insecure: bool,
    timeout: float,
    msg: EmailMessage,
    envelope_from: str,
    envelope_to: Sequence[str],
    verbose_smtp: bool,
) -> None:
    context = ssl.create_default_context()
    if insecure:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        LOG.warning("TLS certificate verification disabled (--insecure). Do not use over untrusted networks.")

    try:
        if use_ssl:
            LOG.debug("Connecting via SMTPS (implicit TLS) to %s:%s", host, port)
            server = smtplib.SMTP_SSL(host, port, timeout=timeout, context=context)
        else:
            LOG.debug("Connecting via SMTP to %s:%s", host, port)
            server = smtplib.SMTP(host, port, timeout=timeout)
    except (OSError, smtplib.SMTPException) as exc:
        raise ConnectionErrorX(f"Could not connect to {host}:{port}: {exc}") from exc

    server.set_debuglevel(1 if verbose_smtp else 0)

    try:
        server.ehlo()
        if use_starttls and not use_ssl:
            if not server.has_extn("starttls"):
                raise ConnectionErrorX(f"Server {host}:{port} does not advertise STARTTLS support.")
            try:
                server.starttls(context=context)
                server.ehlo()
            except smtplib.SMTPException as exc:
                raise ConnectionErrorX(f"STARTTLS negotiation failed: {exc}") from exc

        if not no_auth:
            if not username or password is None:
                raise ConfigError("Username and password are required unless --no-auth is set.")
            try:
                server.login(username, password)
            except smtplib.SMTPAuthenticationError as exc:
                raise AuthError(f"Authentication failed for {username}: {exc}") from exc
            except smtplib.SMTPException as exc:
                raise AuthError(f"Authentication error: {exc}") from exc

        try:
            refused = server.send_message(msg, from_addr=envelope_from, to_addrs=list(envelope_to))
            if refused:
                raise SendError(f"Some recipients were refused by the server: {refused}")
        except smtplib.SMTPRecipientsRefused as exc:
            raise SendError(f"All recipients refused: {exc.recipients}") from exc
        except smtplib.SMTPSenderRefused as exc:
            raise SendError(f"Sender refused: {exc}") from exc
        except smtplib.SMTPDataError as exc:
            raise SendError(f"Server refused message data: {exc}") from exc
        except smtplib.SMTPException as exc:
            raise SendError(f"Send failed: {exc}") from exc
    finally:
        try:
            server.quit()
        except Exception:
            pass

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="zimbra_mailer",
        description="Send an email through a Zimbra (or any standard) SMTP server.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    conn = p.add_argument_group("Connection")
    conn.add_argument("--config", help="Path to an INI config file with a [smtp] section.")
    conn.add_argument("--host", help="SMTP server hostname or IP (e.g. mail.example.com).")
    conn.add_argument("--port", type=int, help="SMTP server port. Defaults: 587 STARTTLS, 465 SSL, 25 plain.")
    conn.add_argument(
        "--security",
        choices=["starttls", "ssl", "none"],
        default=None,
        help="Transport security mode. 'starttls' (typical Zimbra submission, port 587), "
             "'ssl' (implicit TLS/SMTPS, port 465), or 'none' (unencrypted, port 25).",
    )
    conn.add_argument("--timeout", type=float, default=30.0, help="Connection/operation timeout in seconds.")
    conn.add_argument("--insecure", action="store_true", help="Disable TLS certificate verification (not recommended).")

    auth = p.add_argument_group("Authentication")
    auth.add_argument(
        "--email",
        help="Your Zimbra email address — used both as the SMTP auth identity and as the From "
             "address. (Deprecated aliases: --username, --from — still accepted.)",
    )
    auth.add_argument("--username", dest="email", help=argparse.SUPPRESS)
    auth.add_argument("--from", dest="email", help=argparse.SUPPRESS)
    auth.add_argument("--password", help="SMTP auth password. Prefer --password-env or the interactive prompt.")
    auth.add_argument("--password-env", help="Name of an environment variable holding the password.")
    auth.add_argument("--no-auth", action="store_true", help="Skip authentication (open/internal relay setups only).")

    msg = p.add_argument_group("Message")
    msg.add_argument("--from-name", dest="sender_name", help="Display name for the From address.")
    msg.add_argument("--to", action="append", default=[], help="Recipient address. Repeatable.")
    msg.add_argument("--cc", action="append", default=[], help="CC address. Repeatable.")
    msg.add_argument("--bcc", action="append", default=[], help="BCC address (omitted from headers). Repeatable.")
    msg.add_argument("--reply-to", help="Reply-To address.")
    msg.add_argument("--subject", default="", help="Message subject.")
    msg.add_argument("--body", help="Plain-text body given directly on the command line.")
    msg.add_argument("--body-file", help="Path to a file containing the plain-text body.")
    msg.add_argument("--html-body-file", help="Path to a file containing an HTML body.")
    msg.add_argument("--attach", action="append", default=[], help="Path to a file to attach. Repeatable.")

    misc = p.add_argument_group("Behavior")
    misc.add_argument("--dry-run", action="store_true", help="Compose the message and print it, but do not connect or send.")
    misc.add_argument("-v", "--verbose", action="count", default=0, help="Increase logging verbosity (-v, -vv).")
    misc.add_argument("--smtp-debug", action="store_true", help="Log the raw SMTP conversation (may include credentials in some servers' responses).")

    return p


def resolve_setting(cli_value, config: dict, key: str, env_var: Optional[str] = None, default=None):
    if cli_value is not None:
        return cli_value
    if env_var and os.environ.get(env_var) is not None:
        return os.environ[env_var]
    if key in config and config[key] != "":
        return config[key]
    return default


def default_port_for(security: str) -> int:
    return {"starttls": 587, "ssl": 465, "none": 25}[security]


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)

    level = logging.WARNING
    if args.verbose == 1:
        level = logging.INFO
    elif args.verbose >= 2:
        level = logging.DEBUG
    logging.basicConfig(level=level, format="%(levelname)s: %(message)s")

    try:
        config = load_config_file(args.config)

        host = resolve_setting(args.host, config, "host", env_var="ZIMBRA_SMTP_HOST")
        security = resolve_setting(args.security, config, "security", env_var="ZIMBRA_SMTP_SECURITY", default="starttls")
        if security not in ("starttls", "ssl", "none"):
            raise ConfigError(f"Invalid security mode: {security!r}")
        port = resolve_setting(args.port, config, "port", env_var="ZIMBRA_SMTP_PORT")
        port = int(port) if port else default_port_for(security)
        email = resolve_setting(args.email, config, "email", env_var="ZIMBRA_SMTP_EMAIL")
        if email is None:
            email = resolve_setting(None, config, "username", env_var="ZIMBRA_SMTP_USERNAME")
        if email is None:
            email = resolve_setting(None, config, "from", env_var="ZIMBRA_SMTP_FROM")

        no_auth = args.no_auth or str(config.get("no_auth", "")).lower() in ("1", "true", "yes")

        password = args.password
        if password is None and args.password_env:
            password = os.environ.get(args.password_env)
            if password is None:
                raise ConfigError(f"--password-env {args.password_env!r} is not set in the environment.")
        if password is None and not no_auth:
            password = os.environ.get("ZIMBRA_SMTP_PASSWORD")
        if password is None and "password" in config:
            password = config["password"]
        if password is None and not no_auth and not args.dry_run:
            if email is None:
                raise ConfigError("--email (or config/env equivalent) is required unless --no-auth is set.")
            password = getpass.getpass(f"SMTP password for {email}: ")

        sender_name = resolve_setting(args.sender_name, config, "from_name")

        if not host:
            raise ConfigError("--host is required (or set 'host' in config, or ZIMBRA_SMTP_HOST env var).")
        if not email:
            raise ConfigError("--email is required (or set 'email' in config, or ZIMBRA_SMTP_EMAIL env var).")

        body_text = args.body
        if args.body_file:
            body_text = Path(args.body_file).read_text(encoding="utf-8")
        body_html = None
        if args.html_body_file:
            body_html = Path(args.html_body_file).read_text(encoding="utf-8")

        msg = build_message(
            sender=email,
            sender_name=sender_name,
            to=args.to,
            cc=args.cc,
            bcc=args.bcc,
            reply_to=args.reply_to,
            subject=args.subject,
            body_text=body_text,
            body_html=body_html,
            attachments=args.attach,
        )

        envelope_to = all_envelope_recipients(args.to, args.cc, args.bcc)

        if args.dry_run:
            print("----- DRY RUN: composed message (BCC recipients not shown in headers) -----")
            print(msg.as_string())
            print(f"----- Envelope RCPT TO: {envelope_to} -----")
            print(f"----- Would connect to {host}:{port} (security={security}) -----")
            return 0

        send(
            host=host,
            port=port,
            username=email,
            password=password,
            use_ssl=(security == "ssl"),
            use_starttls=(security == "starttls"),
            no_auth=no_auth,
            insecure=args.insecure,
            timeout=args.timeout,
            msg=msg,
            envelope_from=email,
            envelope_to=envelope_to,
            verbose_smtp=args.smtp_debug,
        )

    except MailerError as exc:
        LOG.error(str(exc))
        return exc.exit_code
    except FileNotFoundError as exc:
        LOG.error("File not found: %s", exc)
        return 1
    except KeyboardInterrupt:
        LOG.error("Interrupted.")
        return 130

    LOG.info("Message sent successfully to %s", envelope_to)
    print(f"OK: message sent to {len(envelope_to)} recipient(s) via {host}:{port}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
