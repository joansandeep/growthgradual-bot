"""
POST /api/chat/report/email  — Send the HTML report via Gmail SMTP (smtplib, no extra packages)
Body: {
    sender_email:    str   — Gmail address to send FROM
    app_password:    str   — Gmail App Password (not the account password)
    recipient_email: str   — Address to send TO
    subject:         str   — Email subject line
    report:          str   — Markdown report text
    title:           str   — Report title
    summary:         str   — Executive summary
    keyStats:        list  — [{ label, value, change }]
}
"""
import logging
import smtplib
import html as html_module
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from fastapi import APIRouter
from fastapi.requests import Request
from fastapi.responses import JSONResponse

router = APIRouter()
log = logging.getLogger("email_report")

# ─── Colour palette (matches PDF branding) ────────────────────────────────────
NAVY_HEX  = "#1a1f4e"
GOLD_HEX  = "#c8860a"
BLUE_HEX  = "#3b82f6"
GREEN_HEX = "#22c55e"
RED_HEX   = "#ef4444"
LIGHT_HEX = "#f0f3ff"
GREY_HEX  = "#8b93b5"


def _render_change(change: str) -> str:
    """Return a coloured HTML span for +/- change indicators."""
    if not change:
        return ""
    c = change.strip()
    if c.startswith("+"):
        return f'<span style="color:{GREEN_HEX};font-size:12px;"> {c}</span>'
    if c.startswith("-"):
        return f'<span style="color:{RED_HEX};font-size:12px;"> {c}</span>'
    return f'<span style="color:{GREY_HEX};font-size:12px;"> {c}</span>'


def _markdown_to_html(md: str) -> str:
    """
    Lightweight markdown → HTML converter covering the subset the report uses:
    headers, bold, italic, bullet/numbered lists, tables, horizontal rules,
    [CHART_n] placeholders (stripped), inline code, blockquotes.
    No external dependencies — pure stdlib string processing.
    """
    import re

    lines = md.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    in_ul   = False
    in_ol   = False
    in_table = False
    in_blockquote = False
    table_rows: list[list[str]] = []
    table_header_done = False

    def flush_table():
        nonlocal in_table, table_rows, table_header_done
        if not table_rows:
            return ""
        html_parts = [
            f'<table style="width:100%;border-collapse:collapse;margin:16px 0;font-size:14px;">'
        ]
        for row_idx, row in enumerate(table_rows):
            tag = "th" if row_idx == 0 else "td"
            bg  = NAVY_HEX if row_idx == 0 else ("#f8f9ff" if row_idx % 2 == 0 else "white")
            color = "white" if row_idx == 0 else "#1a1f4e"
            html_parts.append("<tr>")
            for cell in row:
                html_parts.append(
                    f'<{tag} style="border:1px solid #dde3f5;padding:8px 12px;'
                    f'background:{bg};color:{color};text-align:left;">{cell}</{tag}>'
                )
            html_parts.append("</tr>")
        html_parts.append("</table>")
        table_rows.clear()
        table_header_done = False
        in_table = False
        return "".join(html_parts)

    def inline(text: str) -> str:
        """Apply inline markdown formatting."""
        # Strip [CHART_n] placeholders
        text = re.sub(r"\[CHART_\d+\]", "", text)
        # Bold
        text = re.sub(r"\*\*(.+?)\*\*", r'<strong>\1</strong>', text)
        # Italic
        text = re.sub(r"\*(.+?)\*", r'<em>\1</em>', text)
        # Inline code
        text = re.sub(r"`([^`]+)`", r'<code style="background:#f0f3ff;padding:1px 4px;border-radius:3px;font-family:monospace;">\1</code>', text)
        # Citation numbers [1], [2,3] etc.
        text = re.sub(r"\[(\d+(?:,\s*\d+)*)\]", r'<sup style="color:#3b82f6;">[<a href="#references" style="color:#3b82f6;text-decoration:none;">\1</a>]</sup>', text)
        return text

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # ── Blank line / close open blocks ────────────────────────────────────
        if stripped == "":
            if in_ul:
                out.append("</ul>")
                in_ul = False
            if in_ol:
                out.append("</ol>")
                in_ol = False
            if in_table:
                out.append(flush_table())
            if in_blockquote:
                out.append("</blockquote>")
                in_blockquote = False
            out.append("")
            i += 1
            continue

        # ── Table rows ────────────────────────────────────────────────────────
        if stripped.startswith("|") and stripped.endswith("|"):
            # Separator row?
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if all(re.match(r"^[-:]+$", c) for c in cells if c):
                # This is the separator — mark header as done
                table_header_done = True
                in_table = True
                i += 1
                continue
            in_table = True
            table_rows.append([inline(c) for c in cells])
            i += 1
            continue

        # If we were in a table but this line is not a table row, flush
        if in_table:
            out.append(flush_table())

        # ── Horizontal rule ───────────────────────────────────────────────────
        if re.match(r"^[-*_]{3,}$", stripped):
            out.append(f'<hr style="border:none;border-top:1px solid #dde3f5;margin:16px 0;">')
            i += 1
            continue

        # ── Headings ──────────────────────────────────────────────────────────
        m = re.match(r"^(#{1,6})\s+(.*)", stripped)
        if m:
            if in_ul:  out.append("</ul>"); in_ul = False
            if in_ol:  out.append("</ol>"); in_ol = False
            level = len(m.group(1))
            text  = inline(m.group(2))
            if level == 1:
                out.append(
                    f'<h1 style="color:{NAVY_HEX};font-size:22px;margin:24px 0 8px;'
                    f'border-bottom:2px solid {GOLD_HEX};padding-bottom:6px;">{text}</h1>'
                )
            elif level == 2:
                out.append(
                    f'<h2 style="color:{NAVY_HEX};font-size:18px;margin:20px 0 6px;'
                    f'border-left:4px solid {GOLD_HEX};padding-left:10px;">{text}</h2>'
                )
            elif level == 3:
                out.append(
                    f'<h3 style="color:{BLUE_HEX};font-size:15px;margin:14px 0 4px;">{text}</h3>'
                )
            else:
                out.append(f'<h{level} style="color:{NAVY_HEX};font-size:14px;margin:10px 0 4px;">{text}</h{level}>')
            i += 1
            continue

        # ── Blockquote ────────────────────────────────────────────────────────
        if stripped.startswith("> "):
            if not in_blockquote:
                out.append(f'<blockquote style="border-left:4px solid {GOLD_HEX};margin:8px 0;padding:6px 16px;color:#555;background:#fafbff;">')
                in_blockquote = True
            out.append(f'<p style="margin:0;">{inline(stripped[2:])}</p>')
            i += 1
            continue
        elif in_blockquote:
            out.append("</blockquote>")
            in_blockquote = False

        # ── Unordered list ────────────────────────────────────────────────────
        if re.match(r"^[-*+]\s+", stripped):
            if in_ol:  out.append("</ol>"); in_ol = False
            if not in_ul:
                out.append('<ul style="margin:8px 0 8px 20px;padding:0;">')
                in_ul = True
            text = re.sub(r"^[-*+]\s+", "", stripped)
            out.append(f'<li style="margin:3px 0;color:#2d3561;">{inline(text)}</li>')
            i += 1
            continue

        # ── Ordered list ──────────────────────────────────────────────────────
        if re.match(r"^\d+\.\s+", stripped):
            if in_ul:  out.append("</ul>"); in_ul = False
            if not in_ol:
                out.append('<ol style="margin:8px 0 8px 20px;padding:0;">')
                in_ol = True
            text = re.sub(r"^\d+\.\s+", "", stripped)
            out.append(f'<li style="margin:3px 0;color:#2d3561;">{inline(text)}</li>')
            i += 1
            continue

        # Close any open list before a paragraph
        if in_ul:  out.append("</ul>"); in_ul = False
        if in_ol:  out.append("</ol>"); in_ol = False

        # ── Plain paragraph ───────────────────────────────────────────────────
        if stripped:
            out.append(f'<p style="margin:6px 0;line-height:1.65;color:#2d3561;">{inline(stripped)}</p>')

        i += 1

    # Flush any still-open blocks
    if in_ul:         out.append("</ul>")
    if in_ol:         out.append("</ol>")
    if in_table:      out.append(flush_table())
    if in_blockquote: out.append("</blockquote>")

    return "\n".join(out)


def _build_html_email(
    title: str,
    summary: str,
    key_stats: list[dict],
    report_md: str,
    generated_at: str,
) -> str:
    """Assemble a fully self-contained HTML email with inline CSS."""

    # ── Key stats cards ───────────────────────────────────────────────────────
    stat_cards = ""
    if key_stats:
        cards_html = []
        for stat in key_stats[:8]:
            label  = html_module.escape(stat.get("label", ""))
            value  = html_module.escape(stat.get("value", ""))
            change = stat.get("change", "")
            change_html = _render_change(change)
            cards_html.append(f"""
                <td style="padding:8px;">
                  <div style="background:{LIGHT_HEX};border:1px solid #dde3f5;border-radius:8px;
                              padding:14px 16px;min-width:120px;text-align:center;">
                    <div style="font-size:11px;color:{GREY_HEX};text-transform:uppercase;
                                letter-spacing:0.5px;margin-bottom:4px;">{label}</div>
                    <div style="font-size:18px;font-weight:700;color:{NAVY_HEX};">
                      {value}{change_html}
                    </div>
                  </div>
                </td>
            """)
        # Split into rows of 4
        rows_html = []
        for idx in range(0, len(cards_html), 4):
            chunk = cards_html[idx:idx+4]
            rows_html.append(f'<tr>{"".join(chunk)}</tr>')
        stat_cards = f"""
        <div style="margin:24px 0;">
          <h2 style="color:{NAVY_HEX};font-size:16px;margin:0 0 12px;
                     border-left:4px solid {GOLD_HEX};padding-left:10px;">
            Key Statistics
          </h2>
          <table style="border-collapse:separate;border-spacing:0;">
            {"".join(rows_html)}
          </table>
        </div>
        """

    # ── Convert report markdown ───────────────────────────────────────────────
    report_html = _markdown_to_html(report_md) if report_md else ""

    # ── Executive summary block ───────────────────────────────────────────────
    summary_block = ""
    if summary:
        summary_escaped = html_module.escape(summary)
        summary_block = f"""
        <div style="background:{LIGHT_HEX};border-left:4px solid {GOLD_HEX};
                    border-radius:0 8px 8px 0;padding:16px 20px;margin:20px 0;">
          <div style="font-size:11px;color:{GOLD_HEX};text-transform:uppercase;
                      font-weight:600;letter-spacing:0.5px;margin-bottom:6px;">
            Executive Summary
          </div>
          <p style="margin:0;color:{NAVY_HEX};line-height:1.65;font-size:14px;">
            {summary_escaped}
          </p>
        </div>
        """

    title_escaped = html_module.escape(title or "Research Report")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{title_escaped}</title>
</head>
<body style="margin:0;padding:0;background:#eef0f8;font-family:'Segoe UI',Arial,sans-serif;">

  <!-- Outer wrapper -->
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#eef0f8;padding:24px 0;">
    <tr><td align="center">

      <!-- Email card -->
      <table width="680" cellpadding="0" cellspacing="0"
             style="background:white;border-radius:12px;overflow:hidden;
                    box-shadow:0 4px 24px rgba(26,31,78,.12);">

        <!-- ── Header / cover ─────────────────────────────────────── -->
        <tr>
          <td style="background:{NAVY_HEX};padding:36px 40px;">
            <!-- Logo row -->
            <table width="100%" cellpadding="0" cellspacing="0">
              <tr>
                <td>
                  <span style="color:{GOLD_HEX};font-size:22px;font-weight:800;
                               letter-spacing:1px;">Growth Gradual</span>
                  <span style="color:rgba(255,255,255,.5);font-size:13px;
                               margin-left:12px;">In The Money</span>
                </td>
                <td align="right">
                  <span style="background:{GOLD_HEX};color:white;font-size:10px;
                               font-weight:700;padding:3px 10px;border-radius:20px;
                               text-transform:uppercase;letter-spacing:0.5px;">
                    Research Report
                  </span>
                </td>
              </tr>
            </table>

            <!-- Title -->
            <h1 style="color:white;font-size:26px;font-weight:700;margin:24px 0 8px;
                       line-height:1.3;">{title_escaped}</h1>
            <p style="color:rgba(255,255,255,.55);font-size:12px;margin:0;">
              Generated {generated_at} &nbsp;|&nbsp; Growth Gradual AI Research
            </p>
          </td>
        </tr>

        <!-- ── Body ───────────────────────────────────────────────── -->
        <tr>
          <td style="padding:32px 40px;">

            {summary_block}
            {stat_cards}

            <!-- Report body -->
            <div style="margin-top:24px;font-size:14px;line-height:1.7;color:#2d3561;">
              {report_html}
            </div>

          </td>
        </tr>

        <!-- ── Footer ─────────────────────────────────────────────── -->
        <tr>
          <td style="background:{LIGHT_HEX};padding:20px 40px;
                     border-top:1px solid #dde3f5;">
            <table width="100%" cellpadding="0" cellspacing="0">
              <tr>
                <td style="font-size:11px;color:{GREY_HEX};">
                  <strong style="color:{NAVY_HEX};">Growth Gradual</strong> — In The Money<br>
                  This report is AI-generated for informational purposes only.<br>
                  Not financial advice. Verify live prices before trading.
                </td>
                <td align="right" style="font-size:11px;color:{GREY_HEX};">
                  {generated_at}
                </td>
              </tr>
            </table>
          </td>
        </tr>

      </table>
      <!-- /Email card -->

    </td></tr>
  </table>

</body>
</html>"""


@router.post("")
async def send_report_email(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"success": False, "error": "Invalid request body."}, status_code=400)

    sender_email    = (body.get("sender_email")    or "").strip()
    app_password    = (body.get("app_password")    or "").strip()
    recipient_email = (body.get("recipient_email") or "").strip()
    subject         = (body.get("subject")         or "Growth Gradual Research Report").strip()
    report_md       = body.get("report",   "")
    title           = body.get("title",    "Research Report")
    summary         = body.get("summary",  "")
    key_stats       = body.get("keyStats", [])

    # ── Basic validation ──────────────────────────────────────────────────────
    missing = [f for f, v in [
        ("sender_email",    sender_email),
        ("app_password",    app_password),
        ("recipient_email", recipient_email),
    ] if not v]
    if missing:
        return JSONResponse(
            {"success": False, "error": f"Missing required fields: {', '.join(missing)}"},
            status_code=400,
        )

    log.info(
        "Email report: from=%s  to=%s  title=%r",
        sender_email, recipient_email, (title or "")[:60],
    )

    # ── Build the HTML body ───────────────────────────────────────────────────
    now_str = datetime.now(timezone.utc).strftime("%d %b %Y, %I:%M %p UTC")
    html_body = _build_html_email(title, summary, key_stats, report_md, now_str)

    # ── Construct MIME message ────────────────────────────────────────────────
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"Growth Gradual <{sender_email}>"
    msg["To"]      = recipient_email

    # Plain-text fallback (strip markdown tags roughly)
    import re
    plain = re.sub(r"#+\s*", "", report_md)
    plain = re.sub(r"\*+", "", plain)
    plain = re.sub(r"\[CHART_\d+\]", "", plain)
    plain = plain.strip()
    plain_fallback = f"{title}\n{'='*len(title)}\n\n{summary}\n\n{plain}"

    msg.attach(MIMEText(plain_fallback, "plain", "utf-8"))
    msg.attach(MIMEText(html_body,      "html",  "utf-8"))

    # ── Send via Gmail SMTP (TLS on port 587) ─────────────────────────────────
    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=20) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()
            smtp.login(sender_email, app_password)
            smtp.sendmail(sender_email, recipient_email, msg.as_string())

        log.info("Email sent successfully: %s → %s", sender_email, recipient_email)
        return JSONResponse({"success": True, "message": "Report emailed successfully."})

    except smtplib.SMTPAuthenticationError:
        log.warning("SMTP auth failed for %s", sender_email)
        return JSONResponse(
            {
                "success": False,
                "error": (
                    "Gmail authentication failed. Make sure you are using an "
                    "App Password (not your Gmail account password). "
                    "Enable 2FA and generate one at myaccount.google.com/apppasswords."
                ),
            },
            status_code=401,
        )
    except smtplib.SMTPRecipientsRefused:
        return JSONResponse(
            {"success": False, "error": f"Recipient address refused: {recipient_email}"},
            status_code=400,
        )
    except smtplib.SMTPException as exc:
        log.error("SMTP error: %s", exc)
        return JSONResponse({"success": False, "error": f"SMTP error: {exc}"}, status_code=500)
    except OSError as exc:
        log.error("Network error sending email: %s", exc)
        return JSONResponse(
            {"success": False, "error": f"Could not connect to Gmail SMTP: {exc}"},
            status_code=503,
        )
    except Exception as exc:
        log.error("Unexpected error sending email: %s", exc)
        return JSONResponse({"success": False, "error": str(exc)}, status_code=500)
