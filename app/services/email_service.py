"""Outbound email delivery for registration and onboarding flows."""
from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from typing import Any

from app.core.config import settings

logger = logging.getLogger(__name__)


def _smtp_configured() -> bool:
    return bool(
        settings.SMTP_HOST.strip()
        and settings.SMTP_FROM.strip()
        and settings.SMTP_USER.strip()
        and settings.SMTP_PASS.strip()
    )


def _build_registration_email(
    *,
    to_email: str,
    registration_code: str,
    api_key: str,
    pricing_tier_label: str,
    expires_at_iso: str,
    contact: dict[str, str],
) -> EmailMessage:
    from_name = settings.REGISTRATION_CODE_EMAIL_FROM_NAME.strip() or "Hex Zone"
    from_addr = settings.SMTP_FROM.strip()
    msg = EmailMessage()
    msg["Subject"] = f"Your Hex Zone registration code — {pricing_tier_label}"
    msg["From"] = f"{from_name} <{from_addr}>"
    msg["To"] = to_email

    support_name = contact.get("name") or settings.SUPPORT_CONTACT_NAME
    support_email = contact.get("email") or settings.SUPPORT_CONTACT_EMAIL
    support_phone = contact.get("phone") or settings.SUPPORT_CONTACT_PHONE
    support_website = contact.get("website") or settings.SUPPORT_CONTACT_WEBSITE

    plain = f"""Hello,

Your Hex Zone administrator registration is ready.

Registration code (REG-CODE): {registration_code}
API key (pre-allocated):       {api_key}
Pricing tier:                  {pricing_tier_label}
Expires (UTC):                 {expires_at_iso}

Enter the registration code on the Create Account page, then complete signup.
Your API key is active once the account is created.

Support
-------
{support_name}
Email:   {support_email}
Phone:   {support_phone}
Website: {support_website}

— {from_name}
"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<body style="font-family:system-ui,sans-serif;background:#0B0E11;color:#e2e8f0;padding:24px;">
  <div style="max-width:560px;margin:0 auto;background:#151a20;border:1px solid #334155;border-radius:8px;padding:24px;">
    <h1 style="color:#00E5D1;font-size:20px;margin:0 0 16px;">Hex Zone — Registration</h1>
    <p style="color:#94a3b8;">Your administrator registration details:</p>
    <table style="width:100%;border-collapse:collapse;margin:16px 0;">
      <tr><td style="padding:8px 0;color:#64748b;font-size:12px;text-transform:uppercase;">REG-CODE</td></tr>
      <tr><td style="font-family:monospace;font-size:18px;color:#00E5D1;letter-spacing:0.05em;">{registration_code}</td></tr>
      <tr><td style="padding:12px 0 4px;color:#64748b;font-size:12px;text-transform:uppercase;">API key</td></tr>
      <tr><td style="font-family:monospace;font-size:13px;color:#f1f5f9;word-break:break-all;">{api_key}</td></tr>
      <tr><td style="padding:12px 0 4px;color:#64748b;font-size:12px;text-transform:uppercase;">Pricing tier</td></tr>
      <tr><td style="color:#f1f5f9;">{pricing_tier_label}</td></tr>
      <tr><td style="padding:12px 0 4px;color:#64748b;font-size:12px;text-transform:uppercase;">Expires (UTC)</td></tr>
      <tr><td style="color:#f1f5f9;">{expires_at_iso}</td></tr>
    </table>
    <p style="color:#94a3b8;font-size:14px;">Enter the code on the Create Account page to finish signup.</p>
    <hr style="border:none;border-top:1px solid #334155;margin:24px 0;" />
    <p style="color:#64748b;font-size:12px;margin:0;">
      <strong style="color:#94a3b8;">{support_name}</strong><br />
      <a href="mailto:{support_email}" style="color:#00E5D1;">{support_email}</a><br />
      {support_phone}<br />
      <a href="{support_website}" style="color:#00E5D1;">{support_website}</a>
    </p>
  </div>
</body>
</html>"""

    msg.set_content(plain)
    msg.add_alternative(html, subtype="html")
    return msg


def send_registration_code_email(
    *,
    to_email: str,
    registration_code: str,
    api_key: str,
    pricing_tier_label: str,
    expires_at_iso: str,
    contact: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Send REG-CODE + api-key email. Returns delivery metadata for API responses."""
    contact_info = contact or support_contact_dict()
    msg = _build_registration_email(
        to_email=to_email,
        registration_code=registration_code,
        api_key=api_key,
        pricing_tier_label=pricing_tier_label,
        expires_at_iso=expires_at_iso,
        contact=contact_info,
    )

    if not _smtp_configured():
        logger.warning(
            "SMTP not configured — registration email for %s logged only. "
            "REG-CODE=%s expires=%s",
            to_email,
            registration_code,
            expires_at_iso,
        )
        return {
            "sent": False,
            "delivery": "logged",
            "reason": "SMTP not configured (set SMTP_HOST, SMTP_FROM, SMTP_USER, SMTP_PASS)",
        }

    try:
        if settings.SMTP_USE_SSL:
            with smtplib.SMTP_SSL(
                settings.SMTP_HOST.strip(),
                int(settings.SMTP_PORT),
                timeout=settings.SMTP_TIMEOUT_SECONDS,
            ) as smtp:
                smtp.login(settings.SMTP_USER.strip(), settings.SMTP_PASS.strip())
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(
                settings.SMTP_HOST.strip(),
                int(settings.SMTP_PORT),
                timeout=settings.SMTP_TIMEOUT_SECONDS,
            ) as smtp:
                smtp.ehlo()
                smtp.starttls()
                smtp.ehlo()
                smtp.login(settings.SMTP_USER.strip(), settings.SMTP_PASS.strip())
                smtp.send_message(msg)
        logger.info("Registration email sent to %s", to_email)
        return {"sent": True, "delivery": "smtp", "reason": None}
    except Exception as exc:
        logger.exception("Failed to send registration email to %s: %s", to_email, exc)
        return {
            "sent": False,
            "delivery": "failed",
            "reason": str(exc),
        }


def support_contact_dict() -> dict[str, str]:
    return {
        "name": settings.SUPPORT_CONTACT_NAME,
        "email": settings.SUPPORT_CONTACT_EMAIL,
        "phone": settings.SUPPORT_CONTACT_PHONE,
        "website": settings.SUPPORT_CONTACT_WEBSITE,
    }
