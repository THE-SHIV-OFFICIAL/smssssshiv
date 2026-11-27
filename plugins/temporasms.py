"""TemporaSMS integration (https://temporasms.com).

Powers
  WhatsApp  Server 2  -> "Fast & Smart Service"  (operator=smart routing)
  Telegram  Server 3  -> "Change TG No"
  All apps sms        -> every service the panel sells (WhatsApp / Telegram have their own menus)

The API speaks the Hero-SMS / SMS-Activate dialect (handler_api.php), so the whole order
lifecycle in plugins/numflow.py works unchanged: getNumberV2 -> getStatus -> setStatus.

Admin commands
  /tbalance                       panel balance
  /tservices [text]               list app codes (e.g. wa, tg, ig ...)
  /tbuy <service> <country>       get a number manually
  /tstatus <id>  /tcancel <id>  /tdone <id>
  /tempora                        website link
"""
import asyncio
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from config import ADMIN_IDS, TEMPORA_API_KEY, TEMPORA_API_URL, TEMPORA_WEB_URL, logger

# errors that mean "stop, do not retry with other parameters"
_HARD_ERRORS = ("NO_NUMBERS", "NO_BALANCE", "BAD_KEY", "USER_BANNED", "TOO_MANY_REQUESTS", "WRONG_SERVICE",
                "NO_ACTIVATION")

_status_action = {"name": "getStatus"}     # falls back to getSms when the panel rejects getStatus


def tempora_enabled():
    return bool((TEMPORA_API_KEY or "").strip())


# ───────────── low level ─────────────
def _call_sync(params):
    params = {"api_key": TEMPORA_API_KEY, **params}
    url = f"{TEMPORA_API_URL}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (BetaBot)"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read().decode(errors="ignore").strip()
    except urllib.error.HTTPError as ex:      # 4xx bodies carry the real error text
        return ex.read().decode(errors="ignore").strip() or f"HTTP_{ex.code}"


async def tp_call(**params):
    return await asyncio.to_thread(_call_sync, params)


def _json(text):
    try:
        return json.loads(text)
    except Exception:
        return None


def _error_text(res):
    d = _json(res)
    if isinstance(d, dict):
        return str(d.get("title") or d.get("message") or d.get("error") or d.get("status") or res)[:160]
    return str(res)[:160]


# ───────────── code extraction ─────────────
def extract_code(value):
    """The panel may hand back just the code or the whole SMS text. Return only the code."""
    v = str(value or "").strip()
    if not v:
        return v
    if re.fullmatch(r"[A-Za-z0-9\-]{3,10}", v):
        return v.replace("-", "") if re.fullmatch(r"\d{3}-\d{3,4}", v) else v
    m = re.search(r"\b(\d{3})[- ](\d{3})\b", v) or None
    if m:
        return m.group(1) + m.group(2)
    m = re.search(r"\b\d{4,8}\b", v)
    return m.group(0) if m else v


def normalize_status(res):
    """-> 'STATUS_OK:<code>' | 'STATUS_WAIT_CODE' | 'STATUS_CANCEL' | raw error text."""
    res = (res or "").strip()
    if res.startswith("STATUS_OK"):
        return "STATUS_OK:" + extract_code(res.split(":", 1)[1] if ":" in res else "")
    if res.startswith("STATUS_"):
        return res
    d = _json(res)
    if isinstance(d, dict):
        code = d.get("code") or d.get("sms_code") or d.get("otp") or d.get("sms") or d.get("text")
        if isinstance(code, list):
            code = code[0] if code else None
        if isinstance(code, dict):
            code = code.get("code") or code.get("text")
        if code:
            return "STATUS_OK:" + extract_code(code)
        st = str(d.get("status") or "").upper()
        if "CANCEL" in st:
            return "STATUS_CANCEL"
        return "STATUS_WAIT_CODE"
    return res


# ───────────── account ─────────────
async def get_balance():
    res = await tp_call(action="getBalance")
    return res.split(":", 1)[1] if res.startswith("ACCESS_BALANCE") else res


# ───────────── numbers ─────────────
async def get_number(service, country, max_price=None):
    """Buy a number with the smart router. Returns {activationId, phoneNumber, activationCost}; raises on failure."""
    base = {"service": service, "country": country}
    attempts = []
    if max_price:
        attempts.append({**base, "operator": "smart", "maxPrice": max_price})
    attempts.append({**base, "operator": "smart"})
    if max_price:
        attempts.append({**base, "maxPrice": max_price})     # price-first auto mode
    last = "no number"
    for p in attempts:
        res = await tp_call(action="getNumberV2", **p)
        d = _json(res)
        if isinstance(d, dict) and d.get("activationId") is not None:
            return d
        m = re.match(r"ACCESS_NUMBER:(\d+):(\+?\d+)", res or "")
        if m:
            return {"activationId": m.group(1), "phoneNumber": m.group(2)}
        if "BAD_ACTION" in (res or ""):                       # panel without V2 -> classic getNumber
            res = await tp_call(action="getNumber", **p)
            m = re.match(r"ACCESS_NUMBER:(\d+):(\+?\d+)", res or "")
            if m:
                return {"activationId": m.group(1), "phoneNumber": m.group(2)}
        last = _error_text(res)
        if any(h in (res or "") for h in _HARD_ERRORS):
            break
    raise Exception(last)


async def get_status(act_id):
    res = await tp_call(action=_status_action["name"], id=act_id)
    if "BAD_ACTION" in res and _status_action["name"] == "getStatus":
        _status_action["name"] = "getSms"
        res = await tp_call(action="getSms", id=act_id)
    return normalize_status(res)


async def set_status(act_id, status):
    return await tp_call(action="setStatus", id=act_id, status=status)


# ───────────── catalog: apps & countries ─────────────
_services = {"t": 0, "data": []}     # [(code, name)]
_countries = {"t": 0, "data": {}}    # {country_id: english name}

EMOJI = {"ig": "📸", "fb": "📘", "go": "▶️", "tw": "🐦", "tk": "🎵", "ds": "🎮", "vk": "🌐", "ub": "🚗",
         "wa": "💬", "tg": "✈️", "ot": "📲"}


def _parse_services(raw):
    out = []
    data = _json(raw)
    if isinstance(data, dict):
        for k in ("services", "data", "result"):
            if isinstance(data.get(k), (list, dict)):
                data = data[k]
                break
    items = data.items() if isinstance(data, dict) else enumerate(data or [])
    for k, v in items:
        if isinstance(v, dict):
            code = v.get("code") or v.get("id") or v.get("service") or (k if isinstance(k, str) else None)
            name = v.get("name") or v.get("eng") or v.get("title") or v.get("label") or code
        elif isinstance(v, str):
            code, name = (k if isinstance(k, str) else None), v
        else:
            continue
        if code and re.fullmatch(r"[A-Za-z0-9]{1,12}", str(code)):
            out.append((str(code), str(name or code)))
    return out


async def get_services():
    """[(code, name)] of every app the panel supports. Cached 1 h (stale copy kept if the panel is down)."""
    if _services["data"] and time.time() - _services["t"] < 3600:
        return _services["data"]
    try:
        data = _parse_services(await tp_call(action="getServices"))
    except Exception as ex:
        logger.warning("temporasms getServices: %s", ex)
        data = []
    if data:
        _services.update(t=time.time(), data=data)
    return _services["data"]


def service_label(code):
    """(emoji, name) for any app code, usable before the catalog has loaded."""
    for c, n in _services["data"]:
        if c == code:
            return EMOJI.get(code, "📲"), n
    return EMOJI.get(code, "📲"), {"wa": "WhatsApp", "tg": "Telegram", "ig": "Instagram", "fb": "Facebook",
                                    "go": "YouTube / Google"}.get(code, str(code).upper())


async def get_country_names():
    if _countries["data"] and time.time() - _countries["t"] < 3600:
        return _countries["data"]
    data = {}
    try:
        raw = _json(await tp_call(action="getCountries"))
        if isinstance(raw, dict) and isinstance(raw.get("countries"), (list, dict)):
            raw = raw["countries"]
        items = raw.items() if isinstance(raw, dict) else enumerate(raw or [])
        for k, v in items:
            if isinstance(v, dict):
                cid = v.get("id", k)
                data[str(cid)] = v.get("eng") or v.get("name") or v.get("title") or str(cid)
            elif isinstance(v, str):
                data[str(k)] = v
    except Exception as ex:
        logger.warning("temporasms getCountries: %s", ex)
    if data:
        _countries.update(t=time.time(), data=data)
    return _countries["data"]


# ───────────── prices ─────────────
def _offer(d):
    """One price entry -> (cost_usd, count) or None. Handles {cost,count} and {price: count} maps."""
    if not isinstance(d, dict):
        return None
    cost = d.get("cost", d.get("price"))
    if cost is not None:
        cnt = d.get("count", d.get("qty", d.get("stock", d.get("quantity", d.get("physicalCount", 0)))))
        try:
            return float(cost), int(float(cnt or 0))
        except Exception:
            return None
    pairs = []
    for k, v in d.items():
        try:
            pairs.append((float(k), int(float(v))))
        except Exception:
            return None
    pairs = [p for p in pairs if p[1] > 0]
    if not pairs:
        return None
    return min(p[0] for p in pairs), sum(p[1] for p in pairs)


def parse_prices(data, service):
    """[(country_id, cost_usd, count)] cheapest first — tolerant to the shapes the panel may return."""
    out = []
    if isinstance(data, dict) and isinstance(data.get(service), dict) and not _offer(data.get(service)):
        data = data[service]            # {service: {country: offer}}
        pairs = list(data.items())
    elif isinstance(data, dict):
        pairs = [(cid, (svcs or {}).get(service) if isinstance(svcs, dict) and service in svcs
                  else (svcs if _offer(svcs) else None)) for cid, svcs in data.items()]
    else:
        return out
    for cid, offer in pairs:
        o = _offer(offer)
        if o and o[1] > 0:
            out.append((str(cid), o[0], o[1]))
    out.sort(key=lambda x: x[1])
    return out


async def list_prices(service):
    res = await tp_call(action="getPrices", service=service)
    data = _json(res)
    if data is None:
        raise Exception(f"bad response: {res[:120]}")
    rows = parse_prices(data, service)
    if not rows:
        raise Exception("no stock")
    return rows


# ───────────── admin commands ─────────────
def register_temporasms(bot):
    from telethon import events, Button

    def is_admin(e):
        return e.sender_id in ADMIN_IDS

    @bot.on(events.NewMessage(pattern=r"^/tempora$"))
    async def _site(e):
        await e.respond("📲 <b>TemporaSMS</b>\nVirtual numbers for OTP verification",
                        buttons=[Button.url("Open TemporaSMS", TEMPORA_WEB_URL)])

    @bot.on(events.NewMessage(pattern=r"^/tbalance$"))
    async def _bal(e):
        if not is_admin(e): return
        try: await e.respond(f"💰 TemporaSMS balance: <b>${await get_balance()}</b>")
        except Exception as ex: await e.respond(f"❌ {ex}")

    @bot.on(events.NewMessage(pattern=r"^/tservices(?: (.+))?$"))
    async def _svc(e):
        if not is_admin(e): return
        q = (e.pattern_match.group(1) or "").strip().lower()
        rows = [f"<code>{c}</code> {n}" for c, n in await get_services() if not q or q in n.lower() or q == c.lower()]
        await e.respond("\n".join(rows[:80]) or "No services (check TEMPORA_API_KEY).")

    @bot.on(events.NewMessage(pattern=r"^/tbuy (\S+) (\S+)$"))
    async def _buy(e):
        if not is_admin(e): return
        try:
            d = await get_number(e.pattern_match.group(1), e.pattern_match.group(2))
            await e.respond(f"📱 Number: <code>+{str(d['phoneNumber']).lstrip('+')}</code>\n🆔 ID: <code>{d['activationId']}</code>\nUse /tstatus {d['activationId']}")
        except Exception as ex: await e.respond(f"❌ {ex}")

    @bot.on(events.NewMessage(pattern=r"^/tstatus (\d+)$"))
    async def _st(e):
        if not is_admin(e): return
        res = await get_status(e.pattern_match.group(1))
        await e.respond(f"🔢 Code: <code>{res.split(':', 1)[1]}</code>" if res.startswith("STATUS_OK") else f"⏳ {res}")

    @bot.on(events.NewMessage(pattern=r"^/tcancel (\d+)$"))
    async def _cancel(e):
        if not is_admin(e): return
        await e.respond(f"🔴 {await set_status(e.pattern_match.group(1), 8)}")

    @bot.on(events.NewMessage(pattern=r"^/tdone (\d+)$"))
    async def _done(e):
        if not is_admin(e): return
        await e.respond(f"✅ {await set_status(e.pattern_match.group(1), 6)}")

    logger.info("TemporaSMS handlers registered")
