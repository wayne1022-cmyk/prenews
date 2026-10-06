"""
第三階段：寄送 Email。

  正常       → 寄出盤前 AI 摘要
  今天無資料 → 寄出簡短說明（例如休市日）
  任何異常   → 寄出異常通知，附上原因與 Actions 執行連結，不再靜默失敗
  寄信失敗   → 以非零狀態結束，Actions 標示紅色，GitHub 也會通知你
"""
from __future__ import annotations

import html
import os
import re
import smtplib
import sys
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from common import (
    NO_DATA, PARTIAL, RUNNING, STAGE1_FILE, STAGE2_FILE, SUCCESS,
    env_int, env_str, format_date, read_json, require_env, setup_logging, today_tw,
)

log = setup_logging("stage3")

SMTP_SERVER = env_str("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = env_int("SMTP_PORT", 465)
COLOR = "#1F4E79"


def esc(value) -> str:
    return html.escape(str(value or ""), quote=True)


def run_url() -> str:
    """在 GitHub Actions 中執行時，回傳本次執行的網址。"""
    server, repo, run_id = (os.environ.get(k, "") for k in
                            ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID"))
    return f"{server}/{repo}/actions/runs/{run_id}" if repo and run_id else ""


# ============================================================
# 決定要寄哪一種信
# ============================================================

def decide(today: str) -> tuple[str, dict | None, str | None]:
    """回傳 (模式, 資料, 說明)。模式：report / no_data / alert"""
    data = read_json(STAGE2_FILE)

    if data is None:
        s1 = read_json(STAGE1_FILE)
        if s1 is None:
            return "alert", None, "找不到任何階段的輸出檔，流程可能在第一階段之前就失敗了。"
        if s1.get("status") in (SUCCESS, PARTIAL):
            return "alert", s1, "第一階段成功，但第二階段沒有產生結果（可能程式崩潰），請查看 Actions log。"
        data = s1

    if data.get("target_date") != today:
        return "alert", data, f"資料日期 {data.get('target_date')} 與今天 {today} 不符，為避免寄出舊新聞已停止。"

    status = data.get("status")
    if status in (SUCCESS, PARTIAL):
        return "report", data, data.get("reason") if status == PARTIAL else None
    if status == RUNNING:
        return "report", data, "第二階段中途中斷，以下為已完成的部分。"
    if status == NO_DATA:
        return "no_data", data, data.get("reason")
    return "alert", data, data.get("reason") or f"未預期的狀態：{status}"


# ============================================================
# 信件內容（使用 inline style，Gmail 等信箱對 <style> 支援有限）
# ============================================================

def wrap_html(inner: str) -> str:
    return f"""<html><body style="margin:0;padding:0;">
<div style="max-width:860px;margin:auto;font-family:Arial,'Microsoft JhengHei',sans-serif;line-height:1.6;color:#333;">
{inner}
<div style="margin-top:30px;padding-top:15px;border-top:1px solid #ddd;color:#777;font-size:13px;">
本郵件由 CTEE 盤前新聞 AI 自動整理系統產生。新聞來源：工商時報。<br>
AI 摘要僅根據原始文章內容整理，投資決策請以原文及官方資訊為準。
</div></div></body></html>"""


def format_summary(summary: str) -> str:
    parts = []
    for line in esc(summary).splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("【"):
            parts.append(f'<div style="font-weight:bold;color:{COLOR};margin-top:10px;">{line}</div>')
        else:
            parts.append(f'<div style="margin-left:4px;">{line}</div>')
    return "\n".join(parts)


def article_html(index: int, article: dict) -> str:
    status = article.get("ai_status")
    meta = "　".join(x for x in (
        f"作者：{esc(article['author'])}" if article.get("author") else "",
        f"發布時間：{esc(article['publish_time'])}" if article.get("publish_time") else "",
    ) if x)

    if status == "success":
        body = (f'<div style="background:#f7f7f7;padding:12px 15px;border-radius:6px;">'
                f'{format_summary(article["summary"])}</div>')
        notes = []
        if article.get("ai_attempts", 1) > 1:
            notes.append(f"↻ 經 {article['ai_attempts']} 次嘗試後成功")
        if article.get("content_truncated"):
            notes.append("文章較長，AI 僅依前段內容摘要")
        note = (f'<div style="color:#666;font-size:13px;margin-top:8px;">{"；".join(notes)}</div>'
                if notes else "")
    else:
        label = "文章抓取失敗" if status == "skipped" else "AI 摘要失敗"
        error = esc(str(article.get("ai_error") or article.get("scrape_error") or "未知錯誤")[:300])
        body = (f'<div style="background:#fff3f3;border:1px solid #e0aaaa;padding:12px 15px;border-radius:6px;">'
                f'<strong>⚠️ {label}</strong><br>{error}</div>')
        note = ""

    return f"""
<div style="margin-top:22px;padding:18px;border:1px solid #ddd;border-radius:8px;">
  <div style="font-size:18px;font-weight:bold;color:{COLOR};">{index}. {esc(article.get("article_title"))}</div>
  <div style="color:#666;font-size:13px;margin:4px 0 12px;">{meta}</div>
  {body}
  {note}
  <div style="margin-top:12px;"><a href="{esc(article.get('url'))}" style="color:{COLOR};">閱讀工商時報原文</a></div>
</div>"""


def build_report(data: dict, notice: str | None) -> tuple[str, str, str]:
    hub = data.get("hub") or {}
    articles = data.get("articles", [])
    ok = sum(1 for a in articles if a.get("ai_status") == "success")
    date = format_date(data.get("target_date"))

    notice_html = (f'<div style="background:#fff8e1;border:1px solid #f0d58a;padding:10px 15px;'
                   f'border-radius:6px;margin-top:12px;">⚠️ {esc(notice)}</div>' if notice else "")
    header = f"""
<div style="padding:16px 0;border-bottom:2px solid {COLOR};">
  <h1 style="color:{COLOR};margin:0 0 8px;font-size:24px;">工商時報｜盤前新聞 AI 摘要</h1>
  <div>{date}　<a href="{esc(hub.get('url'))}" style="color:{COLOR};">{esc(hub.get('title'))}</a></div>
  <div style="background:#f7f7f7;padding:10px 15px;border-radius:6px;margin-top:12px;">
    共 {len(articles)} 篇，摘要成功 {ok} 篇{f"，未完成 {len(articles) - ok} 篇" if ok < len(articles) else ""}
  </div>
  {notice_html}
</div>"""

    html_body = wrap_html(header + "".join(article_html(i, a) for i, a in enumerate(articles, 1)))

    text_lines = [f"工商時報｜盤前新聞 AI 摘要 {date}", hub.get("title", ""), ""]
    if notice:
        text_lines += [f"⚠️ {notice}", ""]
    for i, a in enumerate(articles, 1):
        text_lines.append(f"{i}. {a.get('article_title', '')}")
        text_lines.append(a.get("summary") or f"（{a.get('ai_error') or a.get('scrape_error') or '未完成'}）")
        text_lines += [a.get("url", ""), ""]

    subject = f"【工商時報｜盤前 AI 摘要】{hub.get('title') or date}"
    return subject, html_body, "\n".join(text_lines)


def build_simple(mode: str, data: dict | None, reason: str | None, today: str) -> tuple[str, str, str]:
    date = format_date((data or {}).get("target_date") or today)
    link = run_url()
    if mode == "no_data":
        subject = f"【盤前摘要】{date} 今日無盤前新聞"
        title, color = "今日沒有找到盤前新聞", COLOR
    else:
        subject = f"【盤前摘要｜執行異常】{date}"
        title, color = "盤前新聞流程執行異常", "#B23B3B"

    link_html = f'<p><a href="{esc(link)}" style="color:{COLOR};">查看本次執行紀錄與除錯檔案</a></p>' if link else ""
    html_body = wrap_html(f"""
<h2 style="color:{color};">{title}</h2>
<p><strong>日期：</strong>{date}</p>
<p><strong>說明：</strong>{esc(reason or "未提供原因")}</p>
{link_html}""")
    text_body = f"{title}\n日期：{date}\n說明：{reason or '未提供原因'}\n{link}"
    return subject, html_body, text_body


# ============================================================
# 寄信
# ============================================================

def parse_recipients(raw: str) -> list[str]:
    """MAIL_TO 可用逗號、分號或空白分隔多個收件者。"""
    return [x for x in re.split(r"[,;\s]+", raw) if x]


def send_email(subject: str, html_body: str, text_body: str) -> None:
    mail_from = require_env("MAIL_FROM")
    password = require_env("MAIL_PASSWORD")
    recipients = parse_recipients(require_env("MAIL_TO"))

    message = MIMEMultipart("alternative")
    message["From"] = mail_from
    message["To"] = ", ".join(recipients)
    message["Subject"] = subject
    message.attach(MIMEText(text_body, "plain", "utf-8"))   # 純文字版：降低被判為垃圾信的機率
    message.attach(MIMEText(html_body, "html", "utf-8"))    # 最後一個是信箱優先顯示的版本

    for attempt in range(1, 4):
        try:
            with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT, timeout=30) as server:
                server.login(mail_from, password)
                server.send_message(message, to_addrs=recipients)
            log.info("✅ 寄送成功：%s → %d 位收件者", subject, len(recipients))
            return
        except smtplib.SMTPAuthenticationError:
            raise  # 帳密錯誤，重試沒有意義
        except (smtplib.SMTPException, OSError) as e:
            if attempt == 3:
                raise
            log.warning("第 %d 次寄送失敗（%s），%d 秒後重試", attempt, e, 10 * attempt)
            time.sleep(10 * attempt)


def main() -> None:
    today = today_tw()
    try:
        mode, data, reason = decide(today)
        log.info("寄送模式：%s｜%s", mode, reason or "正常")
        if mode == "report":
            subject, html_body, text_body = build_report(data, reason)  # type: ignore[arg-type]
        else:
            subject, html_body, text_body = build_simple(mode, data, reason, today)
        send_email(subject, html_body, text_body)
    except Exception:
        log.exception("❌ 第三階段失敗")
        sys.exit(1)


if __name__ == "__main__":
    main()
