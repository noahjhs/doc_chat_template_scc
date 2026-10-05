"""Outgoing email (invitations). Plain SMTP, so any provider works:
SMTP_HOST, SMTP_PORT (587, STARTTLS; 465 means implicit TLS), SMTP_USER,
SMTP_PASSWORD, MAIL_FROM (e.g. "Casper <invites@casperagent.dev>"). A
deployment without SMTP_HOST simply can't send email; callers say so."""

import html
import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import parseaddr


def configured() -> bool:
    return bool(os.environ.get("SMTP_HOST"))


def valid_address(addr: str) -> bool:
    name, email = parseaddr(addr)
    local, _, domain = email.partition("@")
    return bool(local) and "." in domain and email == addr.strip() and not any(c in addr for c in "\r\n,;<>")


def send(to: str, subject: str, text: str, html_body: str, reply_to: str = ""):
    """Raises on failure -- the caller decides how to report it."""
    msg = EmailMessage()
    msg["From"] = os.environ.get("MAIL_FROM", "Casper <invites@casperagent.dev>")
    msg["To"] = to
    msg["Subject"] = subject
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.set_content(text)
    msg.add_alternative(html_body, subtype="html")
    host, port = os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT", "587"))
    context = ssl.create_default_context()
    if port == 465:
        smtp = smtplib.SMTP_SSL(host, port, context=context, timeout=20)
    else:
        smtp = smtplib.SMTP(host, port, timeout=20)
        smtp.starttls(context=context)
    with smtp:
        if os.environ.get("SMTP_USER"):
            smtp.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASSWORD", ""))
        smtp.send_message(msg)


def invite_email(name: str, inviter: str, group: str, gift: str, link: str, prompt: str) -> tuple[str, str, str]:
    """(subject, text, html) for an invitation, in the site's Warm Paper look."""
    hello = f"Welcome, {name}." if name else "Welcome."
    joined = f"You've been invited to join ‘{group}.’" if group else "You've been invited to Casper."
    subject = f"{inviter} invited you to join {group} on Casper" if group else f"{inviter} invited you to Casper"
    text = (
        f"{hello} {joined}\n\n{inviter} has set aside {gift}. Your files are encrypted on your computer before they "
        f"leave it, so {inviter} can never read them.\n\nStart here: {link}\n\n"
        f"Already use an AI agent like Claude Code? Tell it: \"{prompt}\"\n\n"
        f"The invitation works once and expires in a week.\n\nCasper -- held by friends, read by no one."
    )
    e = html.escape
    body = f"""<!doctype html><html><head><meta charset="utf-8"></head><body style="margin:0;background:#f6efe4;font-family:Inter,-apple-system,Helvetica,Arial,sans-serif;color:#2b1d14">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td align="center" style="padding:40px 16px">
<table role="presentation" width="560" cellpadding="0" cellspacing="0" style="max-width:560px;background:#fffaf2;border:1px solid #dccbb5;border-radius:20px">
<tr><td style="padding:40px 40px 8px;font-family:Georgia,serif;font-size:20px;font-weight:600">Casper</td></tr>
<tr><td style="padding:16px 40px 0;font-family:Georgia,serif;font-size:30px;line-height:1.2">{e(hello)}<br><span style="color:#b4572e;font-style:italic">{e(joined)}</span></td></tr>
<tr><td style="padding:20px 40px 0;font-size:16px;line-height:1.6;color:#6f5a4a">{e(inviter)} has set aside {e(gift)}. Your files are encrypted on your computer before they leave it, so {e(inviter)} can never read them.</td></tr>
<tr><td style="padding:28px 40px"><a href="{e(link)}" style="display:inline-block;background:#2b1d14;color:#f6efe4;text-decoration:none;border-radius:999px;padding:13px 24px;font-weight:500">Accept the invitation</a></td></tr>
<tr><td style="padding:0 40px 32px;font-size:14px;line-height:1.6;color:#6f5a4a">Already use an AI agent like Claude Code? Tell it:<br><code style="font-family:Menlo,monospace;font-size:13px;color:#2b1d14">{e(prompt)}</code><br><br>The invitation works once and expires in a week.</td></tr>
</table><p style="font-size:13px;color:#6f5a4a">Casper &mdash; held by friends, read by no one.</p></td></tr></table></body></html>"""
    return subject, text, body
