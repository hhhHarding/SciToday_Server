import subprocess
import logging
import os
from urllib.parse import quote

logger = logging.getLogger(__name__)


def _runtime_profile():
    profile = (os.environ.get("RSSAI_RUNTIME") or "auto").strip().lower()
    if profile not in {"auto", "pc", "termux"}:
        logger.warning("未知 RSSAI_RUNTIME=%r，回退到 auto", profile)
        profile = "auto"
    if profile == "auto":
        return "pc" if os.name == "nt" else "termux"
    return profile


def resolve_notification_channel(config=None):
    configured = (
        ((config or {}).get("notifications") or {}).get("channel")
        if isinstance(config, dict)
        else None
    )
    channel = (
        os.environ.get("RSSAI_NOTIFICATION_CHANNEL")
        or configured
        or "auto"
    )
    channel = str(channel).strip().lower()
    if channel not in {"auto", "termux", "none"}:
        logger.warning("未知通知通道 %r，回退到 auto", channel)
        channel = "auto"
    if channel == "auto":
        return "termux" if _runtime_profile() == "termux" else "none"
    return channel


def send_notification(title, message, url=None, config=None):
    if resolve_notification_channel(config) == "none":
        logger.debug("当前运行环境已禁用系统通知: %s", title)
        return False
    try:
        cmd = ["termux-notification", "--title", title, "--content", message[:500]]
        if url:
            cmd.extend(["--action", f"am start -a android.intent.action.VIEW -d '{url}'"])
        subprocess.run(cmd, check=False, timeout=10)
        logger.info(f"系统通知已发送: {title}")
        return True
    except FileNotFoundError:
        logger.warning("termux-notification 不可用，跳过系统通知")
    except Exception as e:
        logger.error(f"系统通知发送失败: {e}")
    return False


def send_digest_notification(cn_title, keywords, filename, config=None):
    msg = f"中文题目: {cn_title}\n关键词: {keywords}"
    # 通过 App 的 deep link 打开，由 App 用本机 Flask（/inbox）加载摘要。
    # 此前用 file:// 直接指向本地 html，会被浏览器/系统拦截，点击后显示 NOT FOUND。
    # 文件名含中文等字符，必须 URL 编码后才能放进 URI。
    url = f"rssaipush://digest/{quote(filename, safe='')}"
    return send_notification("新论文总结", msg, url, config=config)


def send_pdf_notification(cn_title, keywords, filename, config=None):
    msg = f"中文题目: {cn_title}\n关键词: {keywords}"
    url = f"rssaipush://reading/{quote(filename, safe='')}"
    return send_notification("PDF全文总结", msg, url, config=config)
