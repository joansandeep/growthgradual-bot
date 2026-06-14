"""
POST /api/chat/report/email
Sends the HTML report via Gmail SMTP.
Credentials (SMTP_SENDER and SMTP_APP_PASSWORD) come from environment variables.
Body (multipart/form-data):
  subject      str            — email subject
  recipients   str            — comma-separated emails  OR
  file         UploadFile     — .csv / .xlsx with an "email" column
  report       str            — markdown report text
  title        str            — report title
  summary      str            — executive summary
  keyStats     str (JSON)     — [{ label, value, change }]
"""
import io
import json
import logging
import os
import re
import smtplib
import html as html_module
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import JSONResponse

router = APIRouter()
log = logging.getLogger("email_report")

# ── Palette ───────────────────────────────────────────────────────────────────
NAVY  = "#1a1f4e"
GOLD  = "#c8860a"
BLUE  = "#3b82f6"
GREEN = "#22c55e"
RED   = "#ef4444"
LIGHT = "#f0f3ff"
GREY  = "#8b93b5"


# ── Recipient parsing ─────────────────────────────────────────────────────────

def _emails_from_csv_bytes(data: bytes, filename: str) -> list[str]:
    """Extract emails from CSV or XLSX upload."""
    emails: list[str] = []

    if filename.lower().endswith(".xlsx"):
        try:
            import openpyxl  # type: ignore
            wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
            ws = wb.active
            rows = list(ws.iter_rows(values_only=True))
            if not rows:
                return []
            # Find header row index with "email" column
            header = [str(c).strip().lower() if c else "" for c in rows[0]]
            try:
                col_idx = next(i for i, h in enumerate(header) if "email" in h or "mail" in h)
            except StopIteration:
                col_idx = 0  # fallback: first column
            for row in rows[1:]:
                val = row[col_idx] if col_idx < len(row) else None
                if val and "@" in str(val):
                    emails.append(str(val).strip())
        except Exception as exc:
            log.warning("xlsx parse error: %s", exc)
        return emails

    # CSV / plain text
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if not lines:
        return []
    header_line = lines[0].lower()
    # Detect delimiter
    delim = "\t" if "\t" in header_line else ","
    headers = [h.strip().strip('"') for h in header_line.split(delim)]
    try:
        col_idx = next(i for i, h in enumerate(headers) if "email" in h or "mail" in h)
    except StopIteration:
        col_idx = 0

    for line in lines[1:]:
        parts = [p.strip().strip('"') for p in line.split(delim)]
        if col_idx < len(parts) and "@" in parts[col_idx]:
            emails.append(parts[col_idx])
    return emails


def _parse_recipients(recipients_str: str, file_bytes: bytes, filename: str) -> list[str]:
    result: list[str] = []
    if file_bytes:
        result = _emails_from_csv_bytes(file_bytes, filename)
    if not result and recipients_str:
        result = [e.strip() for e in re.split(r"[,;\s]+", recipients_str) if "@" in e]
    return list(dict.fromkeys(result))  # dedupe, preserve order


# ── HTML email builder ────────────────────────────────────────────────────────

def _change_span(change: str) -> str:
    c = (change or "").strip()
    if c.startswith("+"): return f'<span style="color:{GREEN};font-size:12px;"> {c}</span>'
    if c.startswith("-"): return f'<span style="color:{RED};font-size:12px;"> {c}</span>'
    return f'<span style="color:{GREY};font-size:12px;"> {c}</span>' if c else ""


def _md_to_html(md: str) -> str:
    lines = md.replace("\r\n", "\n").split("\n")
    out, in_ul, in_ol, in_bq, in_table = [], False, False, False, False
    table_rows: list[list[str]] = []

    def flush_table():
        nonlocal in_table, table_rows
        if not table_rows: return ""
        h = ['<table style="width:100%;border-collapse:collapse;margin:16px 0;font-size:14px;">']
        for ri, row in enumerate(table_rows):
            tag = "th" if ri == 0 else "td"
            bg  = NAVY if ri == 0 else ("#f8f9ff" if ri % 2 == 0 else "white")
            col = "white" if ri == 0 else "#1a1f4e"
            h.append("<tr>")
            for cell in row:
                h.append(f'<{tag} style="border:1px solid #dde3f5;padding:8px 12px;background:{bg};color:{col};text-align:left;">{cell}</{tag}>')
            h.append("</tr>")
        h.append("</table>")
        table_rows.clear(); in_table = False
        return "".join(h)

    def inline(t: str) -> str:
        t = re.sub(r"\[CHART_\d+\]", "", t)
        t = re.sub(r"\*\*(.+?)\*\*", r'<strong>\1</strong>', t)
        t = re.sub(r"\*(.+?)\*",     r'<em>\1</em>', t)
        t = re.sub(r"`([^`]+)`", r'<code style="background:#f0f3ff;padding:1px 4px;border-radius:3px;font-family:monospace;">\1</code>', t)
        return t

    i = 0
    while i < len(lines):
        line = lines[i]; s = line.strip()
        if not s:
            if in_ul:  out.append("</ul>"); in_ul = False
            if in_ol:  out.append("</ol>"); in_ol = False
            if in_table: out.append(flush_table())
            if in_bq:  out.append("</blockquote>"); in_bq = False
            i += 1; continue
        if s.startswith("|") and s.endswith("|"):
            cells = [c.strip() for c in s.strip("|").split("|")]
            if all(re.match(r"^[-:]+$", c) for c in cells if c):
                in_table = True; i += 1; continue
            in_table = True; table_rows.append([inline(c) for c in cells]); i += 1; continue
        if in_table: out.append(flush_table())
        if re.match(r"^[-*_]{3,}$", s):
            out.append(f'<hr style="border:none;border-top:1px solid #dde3f5;margin:16px 0;">'); i += 1; continue
        m = re.match(r"^(#{1,6})\s+(.*)", s)
        if m:
            if in_ul: out.append("</ul>"); in_ul = False
            if in_ol: out.append("</ol>"); in_ol = False
            lv = len(m.group(1)); txt = inline(m.group(2))
            if lv == 1:
                out.append(f'<h1 style="color:{NAVY};font-size:22px;margin:24px 0 8px;border-bottom:2px solid {GOLD};padding-bottom:6px;">{txt}</h1>')
            elif lv == 2:
                out.append(f'<h2 style="color:{NAVY};font-size:18px;margin:20px 0 6px;border-left:4px solid {GOLD};padding-left:10px;">{txt}</h2>')
            elif lv == 3:
                out.append(f'<h3 style="color:{BLUE};font-size:15px;margin:14px 0 4px;">{txt}</h3>')
            else:
                out.append(f'<h{lv} style="color:{NAVY};font-size:14px;margin:10px 0 4px;">{txt}</h{lv}>')
            i += 1; continue
        if s.startswith("> "):
            if not in_bq:
                out.append(f'<blockquote style="border-left:4px solid {GOLD};margin:8px 0;padding:6px 16px;color:#555;background:#fafbff;">'); in_bq = True
            out.append(f'<p style="margin:0;">{inline(s[2:])}</p>'); i += 1; continue
        elif in_bq: out.append("</blockquote>"); in_bq = False
        if re.match(r"^[-*+]\s+", s):
            if in_ol: out.append("</ol>"); in_ol = False
            if not in_ul: out.append('<ul style="margin:8px 0 8px 20px;padding:0;">'); in_ul = True
            out.append(f'<li style="margin:3px 0;color:#2d3561;">{inline(re.sub(r"^[-*+]\s+", "", s))}</li>'); i += 1; continue
        if re.match(r"^\d+\.\s+", s):
            if in_ul: out.append("</ul>"); in_ul = False
            if not in_ol: out.append('<ol style="margin:8px 0 8px 20px;padding:0;">'); in_ol = True
            out.append(f'<li style="margin:3px 0;color:#2d3561;">{inline(re.sub(r"^\d+\.\s+", "", s))}</li>'); i += 1; continue
        if in_ul: out.append("</ul>"); in_ul = False
        if in_ol: out.append("</ol>"); in_ol = False
        out.append(f'<p style="margin:6px 0;line-height:1.65;color:#2d3561;">{inline(s)}</p>')
        i += 1

    if in_ul: out.append("</ul>")
    if in_ol: out.append("</ol>")
    if in_table: out.append(flush_table())
    if in_bq: out.append("</blockquote>")
    return "\n".join(out)


def _build_email_html(title: str, summary: str, key_stats: list[dict], report_md: str, ts: str) -> str:
    stat_cards = ""
    if key_stats:
        cards = []
        for s in key_stats[:8]:
            lbl = html_module.escape(s.get("label", ""))
            val = html_module.escape(s.get("value", ""))
            chg = _change_span(s.get("change", ""))
            cards.append(f"""<td style="padding:6px;">
  <div style="background:{LIGHT};border:1px solid #dde3f5;border-radius:8px;padding:12px 14px;min-width:110px;text-align:center;">
    <div style="font-size:10px;color:{GREY};text-transform:uppercase;letter-spacing:.5px;margin-bottom:4px;">{lbl}</div>
    <div style="font-size:17px;font-weight:700;color:{NAVY};">{val}{chg}</div>
  </div></td>""")
        rows = "".join(
            f'<tr>{"".join(cards[j:j+4])}</tr>' for j in range(0, len(cards), 4)
        )
        stat_cards = f"""<div style="margin:20px 0;">
  <h2 style="color:{NAVY};font-size:15px;margin:0 0 10px;border-left:4px solid {GOLD};padding-left:10px;">Key Statistics</h2>
  <table style="border-collapse:separate;border-spacing:0;">{rows}</table></div>"""

    summary_block = ""
    if summary:
        summary_block = f"""<div style="background:{LIGHT};border-left:4px solid {GOLD};border-radius:0 8px 8px 0;padding:14px 18px;margin:18px 0;">
  <div style="font-size:10px;color:{GOLD};text-transform:uppercase;font-weight:600;letter-spacing:.5px;margin-bottom:5px;">Executive Summary</div>
  <p style="margin:0;color:{NAVY};line-height:1.65;font-size:14px;">{html_module.escape(summary)}</p></div>"""

    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html_module.escape(title or 'Report')}</title></head>
<body style="margin:0;padding:0;background:#eef0f8;font-family:'Segoe UI',Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#eef0f8;padding:24px 0;"><tr><td align="center">
<table width="660" cellpadding="0" cellspacing="0" style="background:white;border-radius:12px;overflow:hidden;box-shadow:0 4px 24px rgba(26,31,78,.12);">
  <tr><td style="background:{NAVY};padding:32px 36px;">
    <table width="100%" cellpadding="0" cellspacing="0"><tr>
      <td><span style="color:{GOLD};font-size:20px;font-weight:800;letter-spacing:1px;">Growth Gradual</span>
          <span style="color:rgba(255,255,255,.45);font-size:12px;margin-left:10px;">In The Money</span></td>
      <td align="right"><span style="background:{GOLD};color:white;font-size:9px;font-weight:700;padding:3px 9px;border-radius:20px;text-transform:uppercase;letter-spacing:.5px;">Research Report</span></td>
    </tr></table>
    <h1 style="color:white;font-size:24px;font-weight:700;margin:20px 0 6px;line-height:1.3;">{html_module.escape(title or 'Research Report')}</h1>
    <p style="color:rgba(255,255,255,.5);font-size:11px;margin:0;">Generated {ts} &nbsp;|&nbsp; Growth Gradual AI Research</p>
  </td></tr>
  <tr><td style="padding:28px 36px;">
    {summary_block}
    {stat_cards}
    <div style="margin-top:20px;font-size:14px;line-height:1.7;color:#2d3561;">{_md_to_html(report_md)}</div>
  </td></tr>
  <tr><td style="background:{LIGHT};padding:18px 36px;border-top:1px solid #dde3f5;">
    <table width="100%" cellpadding="0" cellspacing="0"><tr>
      <td style="font-size:10px;color:{GREY};"><strong style="color:{NAVY};">Growth Gradual</strong> — In The Money<br>
        AI-generated report for informational purposes only. Not financial advice.</td>
      <td align="right" style="font-size:10px;color:{GREY};">{ts}</td>
    </tr></table>
  </td></tr>
</table></td></tr></table></body></html>"""


# ── Route ─────────────────────────────────────────────────────────────────────

@router.post("")
async def send_report_email(
    subject:    str           = Form(...),
    recipients: str           = Form(""),
    report:     str           = Form(""),
    title:      str           = Form(""),
    summary:    str           = Form(""),
    keyStats:   str           = Form("[]"),
    file:       Optional[UploadFile] = File(None),
):
    # ── Read SMTP creds from env ───────────────────────────────────────────────
    sender_email = os.environ.get("SMTP_SENDER", "").strip()
    app_password = os.environ.get("SMTP_APP_PASSWORD", "").strip()

    if not sender_email or not app_password:
        return JSONResponse(
            {"success": False, "error": "SMTP credentials not configured on server (SMTP_SENDER / SMTP_APP_PASSWORD missing in .env)."},
            status_code=500,
        )

    # ── Parse recipients ───────────────────────────────────────────────────────
    file_bytes, filename = b"", ""
    if file and file.filename:
        file_bytes = await file.read()
        filename   = file.filename

    to_list = _parse_recipients(recipients, file_bytes, filename)
    if not to_list:
        return JSONResponse({"success": False, "error": "No valid recipient email addresses found."}, status_code=400)

    # ── Parse key stats ────────────────────────────────────────────────────────
    try:
        key_stats = json.loads(keyStats)
    except Exception:
        key_stats = []

    log.info("Email report → %d recipients | title=%r", len(to_list), (title or "")[:60])

    # ── Build HTML ─────────────────────────────────────────────────────────────
    ts       = datetime.now(timezone.utc).strftime("%d %b %Y, %I:%M %p UTC")
    html_body = _build_email_html(title, summary, key_stats, report, ts)

    plain = re.sub(r"#+\s*", "", report)
    plain = re.sub(r"\*+",   "", plain)
    plain = re.sub(r"\[CHART_\d+\]", "", plain).strip()
    plain_body = f"{title}\n{'='*len(title)}\n\n{summary}\n\n{plain}" if title else plain

    # ── Send via Gmail SMTP ────────────────────────────────────────────────────
    # Try port 465 (SSL) first — more reliable on cloud hosts like Render
    # Fall back to port 587 (STARTTLS) if 465 fails
    sent, failed = [], []

    def _do_smtp_send(use_port: int, use_ssl: bool) -> tuple[list, list]:
        _sent, _failed = [], []
        try:
            if use_ssl:
                import ssl as _ssl
                ctx = _ssl.create_default_context()
                smtp_conn = smtplib.SMTP_SSL("smtp.gmail.com", use_port, timeout=20, context=ctx)
            else:
                smtp_conn = smtplib.SMTP("smtp.gmail.com", use_port, timeout=20)
                smtp_conn.ehlo()
                smtp_conn.starttls()
                smtp_conn.ehlo()

            with smtp_conn:
                smtp_conn.login(sender_email, app_password)
                for addr in to_list:
                    try:
                        msg = MIMEMultipart("alternative")
                        msg["Subject"] = subject or "Growth Gradual Research Report"
                        msg["From"]    = f"Growth Gradual <{sender_email}>"
                        msg["To"]      = addr
                        msg.attach(MIMEText(plain_body, "plain", "utf-8"))
                        msg.attach(MIMEText(html_body,  "html",  "utf-8"))
                        smtp_conn.sendmail(sender_email, addr, msg.as_string())
                        _sent.append(addr)
                    except Exception as exc:
                        log.warning("Failed to send to %s: %s", addr, exc)
                        _failed.append(addr)
        except Exception:
            raise
        return _sent, _failed

    last_exc = None
    for port, ssl in [(465, True), (587, False)]:
        try:
            sent, failed = _do_smtp_send(port, ssl)
            log.info("SMTP sent via port %d — sent=%d failed=%d", port, len(sent), len(failed))
            break
        except smtplib.SMTPAuthenticationError:
            return JSONResponse(
                {"success": False, "error": "Gmail authentication failed. Check SMTP_SENDER and SMTP_APP_PASSWORD in your .env (use an App Password, not your account password)."},
                status_code=401,
            )
        except Exception as exc:
            log.warning("SMTP port %d failed: %s — trying next", port, exc)
            last_exc = exc
            continue
    else:
        return JSONResponse({"success": False, "error": f"Could not connect to Gmail SMTP on any port: {last_exc}"}, status_code=503)

    if not sent:
        return JSONResponse({"success": False, "error": f"All sends failed. Failed: {', '.join(failed)}"}, status_code=500)

    msg_parts = [f"Report sent to {len(sent)} recipient{'s' if len(sent) > 1 else ''}."]
    if failed:
        msg_parts.append(f"Failed: {', '.join(failed)}")

    return JSONResponse({"success": True, "sent": sent, "failed": failed, "message": " ".join(msg_parts)})
