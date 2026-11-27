"""Public 'Buy Logger' — posts a professional purchase proof to the store channel.

Rules
  * A log is posted only AFTER the buyer really received the item (OTP / account / file).
  * When the buyer is known the very same log message is forwarded to them as their confirmation.
  * OTP codes are shown inside a spoiler (see config.SALE_LOG_SHOW_CODE).
"""
import html
import time
from telethon import Button
from config import bot, logger, SALE_LOG_CHANNEL, PE_CHECK, SUPPORT_USERNAME, SALE_LOG_SHOW_CODE

_ME = {"u": None}


async def _bot_username():
    if not _ME["u"]:
        me = await bot.get_me()
        _ME["u"] = me.username or ""
    return _ME["u"]


def code_spoiler(code, dots="●●●●●"):
    """HTML for the 'Code' line: the real code inside a spoiler, or dots when disabled / unknown."""
    if code and SALE_LOG_SHOW_CODE:
        return f"<tg-spoiler>{html.escape(str(code))}</tg-spoiler>"
    return f"<tg-spoiler>{dots}</tg-spoiler>"


async def _send(chat, msg, **kw):
    """Send with the premium emoji; retry as plain text. Returns the Message or None."""
    from utils.logsend import plain
    try:
        return await bot.send_message(chat, msg, link_preview=False, **kw)
    except Exception as ex:
        first = ex
    try:
        return await bot.send_message(chat, plain(msg), link_preview=False, **kw)
    except Exception as ex:
        logger.warning("Sale log send to %s failed: %s / %s (is the bot admin there?)", chat, first, ex)
        return None


async def post_sale(title, body, buyer_id=None):
    """Post the purchase proof to the channel and (if buyer_id) forward it to the buyer.
    Never raises. Returns the channel message (or None)."""
    uname = await _bot_username()
    ts = time.strftime("%d %b %Y • %I:%M %p")
    msg = (f"<blockquote><b>{PE_CHECK} {title}</b></blockquote>\n\n"
           f"{body}\n\n"
           f"🕒 <i>{ts}</i>\n"
           f"• <a href='https://t.me/{uname}'>@{uname}</a> || "
           f"<a href='https://t.me/BETABOT_HUB'>@BETABOT_HUB</a> || "
           f"<a href='https://t.me/{SUPPORT_USERNAME}'>@{SUPPORT_USERNAME}</a>")
    buttons = [[Button.url("• Buy Now •", f"https://t.me/{uname}?start=store", style="success", icon=5440627033111557670)]] if uname else None
    kw = {"buttons": buttons} if buttons else {}

    sent = await _send(SALE_LOG_CHANNEL, msg, **kw) if SALE_LOG_CHANNEL else None

    if buyer_id:
        done = False
        if sent is not None:
            try:      # real forward -> shows "Forwarded from <store channel>" = proof for the buyer
                await bot.forward_messages(buyer_id, sent.id, SALE_LOG_CHANNEL)
                done = True
            except Exception as ex:
                logger.info("sale log forward to %s failed (%s) - sending a copy", buyer_id, ex)
        if not done:   # protected channel / no channel configured -> send the same text as a copy
            await _send(buyer_id, msg, **kw)
    return sent
