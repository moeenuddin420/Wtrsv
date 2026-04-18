"""
Combined WTS + WTR Render Server
Single Telethon session handles both @WStaskbot and @wsotp200bot
"""

import asyncio
import json
import os
import re
import time
import hmac
import hashlib
from contextlib import asynccontextmanager

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from telethon import TelegramClient, events
from telethon.sessions import StringSession

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

TG_API_ID        = int(os.environ["TG_API_ID"])
TG_API_HASH      = os.environ["TG_API_HASH"]
TG_SESSION       = os.environ.get("TG_SESSION", "")
TG_PHONE         = os.environ.get("TG_PHONE", "")
WTS_BOT_USERNAME = os.environ.get("WTS_BOT_USERNAME", "@WStaskbot")
WTR_BOT_USERNAME = os.environ.get("WTR_BOT_USERNAME", "@wsotp200bot")
CF_URL           = os.environ["CF_WORKER_URL"].rstrip("/")
SECRET           = os.environ["INTERNAL_SECRET"]

# ─────────────────────────────────────────────
# TELETHON CLIENT
# ─────────────────────────────────────────────

tg_client = TelegramClient(
    StringSession(TG_SESSION),
    TG_API_ID,
    TG_API_HASH
)

# Resolved bot entity IDs — set at startup, used for reliable chat filtering
WTS_BOT_ID = None
WTR_BOT_ID = None

# ─────────────────────────────────────────────
# WTR IN-MEMORY TRACKING
#
# Key:   phone number string e.g. "8801616632459"
# Value: {
#   progress_count:  0 | 1 | 2
#   progress2_msg:   Telethon Message object (to reply OTP to)
#   status:          "waiting"|"progress1"|"progress2"|"success"|"failed"
#   otp_submitted:   bool
#   registration_id: str
#   timeout_task:    asyncio.Task | None
# }
# ─────────────────────────────────────────────

wtr_tracking: dict = {}
wtr_lock = asyncio.Lock()
OTP_TIMEOUT_SECS = 360  # 6 minutes

# ─────────────────────────────────────────────
# CF CALLER
# ─────────────────────────────────────────────

async def call_cf(path: str, body: dict, wtr_sig: bool = False):
    try:
        payload = json.dumps(body, separators=(",", ":"), sort_keys=True)
        headers = {"Content-Type": "application/json"}
        if wtr_sig:
            sig = hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
            headers["X-WTR-Signature"] = sig
        else:
            headers["X-Internal-Secret"] = SECRET
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(f"{CF_URL}{path}", content=payload, headers=headers)
            print(f"[CF] {path} → {r.status_code}")
            return r.json() if r.text else {}
    except Exception as e:
        print(f"[CF] failed {path}: {e}")
        return {}

# ─────────────────────────────────────────────
# WTS — MESSAGE PARSERS
# ─────────────────────────────────────────────

def wts_extract_number_from_block(text: str) -> str | None:
    """
    Extracts number from the dashed block pattern:
    ✅ Sending Task Completed
    --------------------
    8801XXXXXXXXX          ← this line
    --------------------
    """
    lines = text.split("\n")
    for i, line in enumerate(lines):
        s = line.strip()
        if "----" in s:
            # Check the next line for a phone number
            if i + 1 < len(lines):
                candidate = lines[i + 1].strip()
                if re.fullmatch(r'\d{7,15}', candidate):
                    return candidate
    return None

def wts_parse_task_completion(text: str) -> tuple | None:
    """Returns (number, total_sent) or None."""
    if "Task Completed" not in text or "successfully sent" not in text:
        return None
    number = wts_extract_number_from_block(text)
    if not number:
        m = re.search(r'\b(\d{7,15})\b', text)
        number = m.group(1) if m else None
    total = re.search(r'Total successfully sent:\s*(\d+)', text)
    if number and total:
        return number, int(total.group(1))
    return None

def wts_parse_auth_failed(text: str) -> str | None:
    """Returns number or None when auth failed message received."""
    if "Authorization failed" not in text:
        return None
    number = wts_extract_number_from_block(text)
    if not number:
        m = re.search(r'\b(\d{7,15})\b', text)
        number = m.group(1) if m else None
    return number

def wts_parse_login_success(text: str) -> str | None:
    """
    Detects login success message:
    Account has logged in successfully...
    Number: 8801XXXXXXXXX
    Status: Online
    """
    if "logged in successfully" not in text.lower():
        return None
    m = re.search(r'Number:\s*(\d{7,15})', text)
    return m.group(1) if m else None

def wts_parse_pairing_code(text: str) -> str | None:
    """Finds XXXX-XXXX pairing code anywhere in text."""
    m = re.search(r'\b(\d{4}-\d{4})\b', text)
    return m.group(1) if m else None

# ─────────────────────────────────────────────
# WTR — MESSAGE PARSERS
# ─────────────────────────────────────────────

def wtr_number_at_start(text: str) -> str | None:
    """
    Phone number at the very start of message.
    e.g. "8801616632459 🔵 In Progress"
    e.g. "8801616632459 🟡 Try later"
    e.g. "8801616632459 🟢 Success"
    """
    m = re.match(r'^(\d{7,15})\b', text.strip())
    return m.group(1) if m else None

def wtr_number_anywhere(text: str) -> str | None:
    """
    Finds number in reward notification or rate-limit messages.
    Checks 'Number: XXXXX' pattern first, then standalone digit line.
    """
    m = re.search(r'Number:\s*(\d{7,15})', text)
    if m:
        return m.group(1)
    # Rate-limit format: number on its own line before dashes
    for line in text.strip().split("\n"):
        s = line.strip()
        if re.fullmatch(r'\d{7,15}', s):
            return s
    return None

def wtr_classify(text: str) -> str | None:
    t = text.strip()
    if "🔵" in t and "In Progress" in t:
        return "progress"
    if "🟢" in t and "Success" in t:
        return "number_success"
    if "💰" in t and "New Reward Notification" in t:
        return "reward"
    if "🟡" in t and "Try later" in t:
        return "try_later"
    if "please submit this number again in" in t.lower():
        return "retry_later"
    if "verification code can only be 6 digits" in t.lower():
        return "wrong_otp_format"
    if "this number is wrong" in t.lower() or "country code is not supported" in t.lower():
        return "invalid_number"
    return None

# ─────────────────────────────────────────────
# WTR — TIMEOUT HANDLER
# ─────────────────────────────────────────────

async def wtr_handle_timeout(number: str, registration_id: str):
    await asyncio.sleep(OTP_TIMEOUT_SECS)
    async with wtr_lock:
        entry = wtr_tracking.get(number)
        if not entry or entry["status"] in ("success", "failed"):
            return
        entry["status"] = "failed"
        wtr_tracking.pop(number, None)
    print(f"[WTR] Timeout for {number}")
    await call_cf(
        "/wtr/callback",
        {"number": number, "registration_id": registration_id, "event": "timeout"},
        wtr_sig=True
    )

# ─────────────────────────────────────────────
# TELEGRAM EVENT HANDLER
# Filters by resolved bot entity ID — 100% reliable
# ─────────────────────────────────────────────

@tg_client.on(events.NewMessage())
async def on_message(event):
    chat_id = event.chat_id
    msg     = event.message
    text    = (msg.message or "").strip()
    msg_id  = msg.id

    # ── WTS BOT ──────────────────────────────────────────────────────
    if chat_id == WTS_BOT_ID:
        print(f"[WTS] msg={msg_id} | {text[:140]}")

        # Task completed — credit user
        result = wts_parse_task_completion(text)
        if result:
            number, total_sent = result
            asyncio.create_task(call_cf("/tg-event", {
                "type": "task_complete",
                "data": {"whatsapp_number": number, "total_sent": total_sent, "raw_message": text}
            }))
            return

        # Auth failed — mark number removed, reset stats
        number = wts_parse_auth_failed(text)
        if number:
            asyncio.create_task(call_cf("/tg-event", {
                "type": "number_removed",
                "data": {"whatsapp_number": number}
            }))
            return

    # ── WTR BOT ──────────────────────────────────────────────────────
    elif chat_id == WTR_BOT_ID:
        print(f"[WTR] msg={msg_id} | {text[:140]}")

        event_type = wtr_classify(text)
        if not event_type:
            return

        # Reward notification — new separate message, contains number in body
        if event_type == "reward":
            number = wtr_number_anywhere(text)
            if not number:
                return
            async with wtr_lock:
                entry  = wtr_tracking.get(number)
                reg_id = entry["registration_id"] if entry else None
                if entry:
                    if entry.get("timeout_task"):
                        entry["timeout_task"].cancel()
                    entry["status"] = "success"
                    wtr_tracking.pop(number, None)
            if reg_id:
                asyncio.create_task(call_cf(
                    "/wtr/callback",
                    {"number": number, "registration_id": reg_id, "event": "success"},
                    wtr_sig=True
                ))
            return

        # All other WTR events have number at start of message
        number = wtr_number_at_start(text)

        # Rate-limit message: number may be on its own line not inline
        if not number and event_type == "retry_later":
            number = wtr_number_anywhere(text)

        if not number:
            return

        async with wtr_lock:
            entry = wtr_tracking.get(number)
            if not entry:
                # Not a number we submitted — ignore
                return
            reg_id = entry["registration_id"]

            # ── Progress message ──────────────────────────────────
            if event_type == "progress":
                entry["progress_count"] += 1
                count = entry["progress_count"]

                if count == 1:
                    # 1st progress: number is valid, bot is sending OTP to WhatsApp
                    entry["status"] = "progress1"
                    print(f"[WTR] {number} → Progress 1 (OTP being dispatched)")
                    # Wait silently — either progress2 or try_later will follow

                elif count == 2:
                    # 2nd progress: OTP has been sent to user's WhatsApp
                    # Store the actual message object — we MUST reply to this exact message
                    entry["progress2_msg"] = msg
                    entry["status"]        = "progress2"
                    print(f"[WTR] {number} → Progress 2 (msg_id={msg_id}, awaiting OTP)")
                    # Start 6-minute OTP window timeout
                    if entry.get("timeout_task"):
                        entry["timeout_task"].cancel()
                    entry["timeout_task"] = asyncio.create_task(
                        wtr_handle_timeout(number, reg_id)
                    )
                    # Tell CF to ask user for OTP
                    asyncio.create_task(call_cf(
                        "/wtr/callback",
                        {"number": number, "registration_id": reg_id, "event": "needs_otp"},
                        wtr_sig=True
                    ))

            # ── Try Later ─────────────────────────────────────────
            elif event_type == "try_later":
                # After progress1: number unusable right now
                # After OTP reply: wrong OTP was entered
                reason = "wrong_otp" if entry.get("otp_submitted") else "try_later"
                entry["status"] = "failed"
                if entry.get("timeout_task"):
                    entry["timeout_task"].cancel()
                wtr_tracking.pop(number, None)
                print(f"[WTR] {number} → Try Later (reason={reason})")
                asyncio.create_task(call_cf(
                    "/wtr/callback",
                    {"number": number, "registration_id": reg_id,
                     "event": "failed", "reason": reason},
                    wtr_sig=True
                ))

            # ── Rate Limited ──────────────────────────────────────
            elif event_type == "retry_later":
                m = re.search(r'(\d+)\s*second', text, re.IGNORECASE)
                wait = int(m.group(1)) if m else 6
                print(f"[WTR] {number} → Retry later in {wait}s")
                asyncio.create_task(call_cf(
                    "/wtr/callback",
                    {"number": number, "registration_id": reg_id,
                     "event": "retry_later", "wait_seconds": wait},
                    wtr_sig=True
                ))

            # ── Wrong OTP Format ──────────────────────────────────
            elif event_type == "wrong_otp_format":
                print(f"[WTR] {number} → Wrong OTP format")
                asyncio.create_task(call_cf(
                    "/wtr/callback",
                    {"number": number, "registration_id": reg_id,
                     "event": "wrong_otp_format"},
                    wtr_sig=True
                ))

            # ── Invalid Number ────────────────────────────────────
            elif event_type == "invalid_number":
                entry["status"] = "failed"
                if entry.get("timeout_task"):
                    entry["timeout_task"].cancel()
                wtr_tracking.pop(number, None)
                print(f"[WTR] {number} → Invalid number/country")
                asyncio.create_task(call_cf(
                    "/wtr/callback",
                    {"number": number, "registration_id": reg_id,
                     "event": "invalid_number"},
                    wtr_sig=True
                ))

            # ── Number Success (🟢 Success) via new message ───────
            elif event_type == "number_success":
                # OTP accepted — reward notification follows immediately as a new message
                # Passive listener will catch reward message and credit balance
                entry["status"] = "success"
                if entry.get("timeout_task"):
                    entry["timeout_task"].cancel()
                print(f"[WTR] {number} → Success confirmed, awaiting reward msg")

# ─────────────────────────────────────────────
# WTR — MESSAGE EDITED HANDLER
#
# The bot EDITS (not sends new) in these cases:
#   progress 1 → "🟡 Try later"   (number unusable)
#   progress 2 → "🟡 Try later"   (wrong OTP)
#   progress 2 → "🟢 Success"     (correct OTP)
#
# We must listen to MessageEdited for WTR bot only.
# ─────────────────────────────────────────────

@tg_client.on(events.MessageEdited())
async def on_message_edited(event):
    # Only care about edits from the WTR bot
    if event.chat_id != WTR_BOT_ID:
        return

    msg  = event.message
    text = (msg.message or "").strip()
    print(f"[WTR EDIT] msg={msg.id} | {text[:120]}")

    event_type = wtr_classify(text)
    if event_type not in ("try_later", "number_success"):
        # Only these two matter as edits
        return

    number = wtr_number_at_start(text)
    if not number:
        return

    async with wtr_lock:
        entry = wtr_tracking.get(number)
        if not entry:
            return
        reg_id = entry["registration_id"]

        if event_type == "try_later":
            # Edited to try_later means:
            #   - After progress 1 edit: number is unusable right now
            #   - After progress 2 edit: wrong OTP was entered
            reason = "wrong_otp" if entry.get("otp_submitted") else "try_later"
            entry["status"] = "failed"
            if entry.get("timeout_task"):
                entry["timeout_task"].cancel()
            wtr_tracking.pop(number, None)
            print(f"[WTR] {number} → Try Later via edit (reason={reason})")
            asyncio.create_task(call_cf(
                "/wtr/callback",
                {"number": number, "registration_id": reg_id,
                 "event": "failed", "reason": reason},
                wtr_sig=True
            ))

        elif event_type == "number_success":
            # Progress 2 edited to 🟢 Success — OTP was correct
            # Reward notification new message will follow and credit balance
            entry["status"] = "success"
            if entry.get("timeout_task"):
                entry["timeout_task"].cancel()
            print(f"[WTR] {number} → Success via edit, awaiting reward msg")

# ─────────────────────────────────────────────
# FASTAPI APP
# ─────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global WTS_BOT_ID, WTR_BOT_ID
    print("[Server] Connecting Telethon...")
    await tg_client.start(phone=TG_PHONE or None)

    # Resolve bot entity IDs once at startup — critical for reliable filtering
    wts_ent    = await tg_client.get_entity(WTS_BOT_USERNAME)
    wtr_ent    = await tg_client.get_entity(WTR_BOT_USERNAME)
    WTS_BOT_ID = wts_ent.id
    WTR_BOT_ID = wtr_ent.id
    print(f"[Server] WTS bot id={WTS_BOT_ID}")
    print(f"[Server] WTR bot id={WTR_BOT_ID}")
    print(f"[Server] Listening to both bots")
    yield
    await tg_client.disconnect()

app = FastAPI(title="WTS+WTR Server", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ─────────────────────────────────────────────
# AUTH HELPER
# ─────────────────────────────────────────────

def check_internal(req: Request):
    if req.headers.get("X-Internal-Secret", "") != SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")

# ─────────────────────────────────────────────
# WTS — ADD NUMBER (SSE)
# ─────────────────────────────────────────────

class AddNumberReq(BaseModel):
    whatsapp_number: str
    account_type: str
    limit: str
    user_id: str

async def add_number_flow(whatsapp_number: str, account_type: str, limit: str, user_id: str):
    def sse(event: str, data: str) -> str:
        return f"event: {event}\ndata: {data}\n\n"

    try:
        bot = await tg_client.get_entity(WTS_BOT_USERNAME)

        # 1 — Send number
        yield sse("status", "Sending number to bot...")
        await tg_client.send_message(bot, whatsapp_number)
        await asyncio.sleep(5)

        # 2 — Click account type button
        msgs = await tg_client.get_messages(bot, limit=3)
        msg  = next((m for m in msgs if m.buttons), None)
        if not msg:
            yield sse("error", "Bot did not respond with account type buttons.")
            return

        yield sse("status", f"Selecting {account_type}...")
        clicked = False
        for row in msg.buttons:
            for btn in row:
                if account_type.lower() in btn.text.lower():
                    await btn.click()
                    clicked = True
                    break
            if clicked:
                break
        if not clicked:
            yield sse("error", f"Could not find '{account_type}' button.")
            return

        await asyncio.sleep(5)

        # 3 — Click limit button
        msgs = await tg_client.get_messages(bot, limit=3)
        msg  = next((m for m in msgs if m.buttons), None)
        if not msg:
            yield sse("error", "Bot did not respond with limit buttons.")
            return

        yield sse("status", f"Setting limit to {limit}...")
        clicked = False
        for row in msg.buttons:
            for btn in row:
                if btn.text.strip() == str(limit):
                    await btn.click()
                    clicked = True
                    break
            if clicked:
                break
        if not clicked:
            yield sse("error", f"Could not find limit '{limit}' button.")
            return

        await asyncio.sleep(6)

        # 4 — Wait for pairing code
        # Check last 3 messages every 3 seconds for up to 60 seconds
        yield sse("status", "Waiting for pairing code...")
        pairing_code = None
        for _ in range(20):
            msgs = await tg_client.get_messages(bot, limit=3)
            for m in msgs:
                code = wts_parse_pairing_code(m.message or "")
                if code:
                    pairing_code = code
                    break
            if pairing_code:
                break
            await asyncio.sleep(3)

        if not pairing_code:
            yield sse("error", "Pairing code not received. Please try again.")
            return

        # Mark number pending in DB
        daily_limit = int(limit) if limit != "NoLimit" else 999
        await call_cf("/tg-event", {
            "type": "number_pending",
            "data": {
                "user_id":         user_id,
                "whatsapp_number": whatsapp_number,
                "account_type":    account_type,
                "daily_limit":     daily_limit
            }
        })

        # Send pairing code to frontend
        yield sse("pairing_code", pairing_code)
        yield sse("status", "Enter code in WhatsApp → Linked Devices. Waiting up to 4 minutes...")

        # 5 — Poll for login success confirmation every 5 seconds for up to 4 minutes
        for _ in range(48):
            msgs = await tg_client.get_messages(bot, limit=3)
            for m in msgs:
                success_number = wts_parse_login_success(m.message or "")
                if success_number and success_number == whatsapp_number:
                    await call_cf("/tg-event", {
                        "type": "number_added",
                        "data": {
                            "user_id":         user_id,
                            "whatsapp_number": whatsapp_number,
                            "account_type":    account_type,
                            "daily_limit":     daily_limit
                        }
                    })
                    yield sse("success", "Number added successfully ✅")
                    return
            await asyncio.sleep(5)

        yield sse("error", "Timed out waiting for WhatsApp confirmation. Code may have expired.")

    except Exception as e:
        print(f"[WTS] add_number_flow error: {e}")
        yield sse("error", f"Error: {str(e)}")

@app.post("/add-number")
async def add_number(req: AddNumberReq):
    return StreamingResponse(
        add_number_flow(req.whatsapp_number, req.account_type, req.limit, req.user_id),
        media_type="text/event-stream"
    )

# ─────────────────────────────────────────────
# WTR — SEND NUMBER TO BOT
# ─────────────────────────────────────────────

class SendNumberReq(BaseModel):
    number: str
    registration_id: str

@app.post("/wtr/send-number")
async def wtr_send_number(req: SendNumberReq, request: Request):
    check_internal(request)
    number = req.number.strip().lstrip("+")
    if not number.isdigit() or not (7 <= len(number) <= 15):
        raise HTTPException(400, "Invalid number format")

    async with wtr_lock:
        if number in wtr_tracking:
            raise HTTPException(409, "Number already being tracked")
        wtr_tracking[number] = {
            "progress_count":  0,
            "progress2_msg":   None,
            "status":          "waiting",
            "otp_submitted":   False,
            "registration_id": req.registration_id,
            "timeout_task":    None,
        }

    try:
        # Send directly to bot using resolved entity ID
        await tg_client.send_message(WTR_BOT_ID, number)
        print(f"[WTR] Sent {number} to bot")
    except Exception as e:
        async with wtr_lock:
            wtr_tracking.pop(number, None)
        raise HTTPException(500, f"Failed to send to bot: {e}")

    return {"status": "sent", "number": number}

# ─────────────────────────────────────────────
# WTR — SEND OTP (reply to 2nd progress message)
# ─────────────────────────────────────────────

class SendOtpReq(BaseModel):
    number: str
    otp: str
    registration_id: str

@app.post("/wtr/send-otp")
async def wtr_send_otp(req: SendOtpReq, request: Request):
    check_internal(request)
    number = req.number.strip().lstrip("+")
    otp    = req.otp.strip()

    if not re.fullmatch(r'\d{6}', otp):
        raise HTTPException(400, "OTP must be exactly 6 digits")

    async with wtr_lock:
        entry = wtr_tracking.get(number)
        if not entry:
            raise HTTPException(404, "Number not being tracked")
        if entry["status"] != "progress2":
            raise HTTPException(409, f"Not awaiting OTP (status={entry['status']})")
        msg = entry.get("progress2_msg")
        if not msg:
            raise HTTPException(500, "Progress 2 message object missing")
        entry["otp_submitted"] = True

    try:
        # Reply to the exact 2nd progress message — critical
        await tg_client.send_message(WTR_BOT_ID, otp, reply_to=msg.id)
        print(f"[WTR] OTP sent for {number} replying to msg_id={msg.id}")
    except Exception as e:
        raise HTTPException(500, f"Failed to send OTP: {e}")

    return {"status": "otp_sent"}

# ─────────────────────────────────────────────
# WTR — STATUS
# ─────────────────────────────────────────────

@app.get("/wtr/status/{number}")
async def wtr_status(number: str, request: Request):
    check_internal(request)
    number = number.strip().lstrip("+")
    async with wtr_lock:
        entry = wtr_tracking.get(number)
    if not entry:
        return {"number": number, "status": "not_tracked"}
    return {
        "number":         number,
        "status":         entry["status"],
        "progress_count": entry["progress_count"],
        "has_otp_msg":    entry["progress2_msg"] is not None,
    }

# ─────────────────────────────────────────────
# HEALTH / PING
# ─────────────────────────────────────────────

@app.get("/health")
async def health():
    return {
        "status":       "ok",
        "tg_connected": tg_client.is_connected(),
        "wtr_tracked":  len(wtr_tracking),
    }

@app.get("/ping")
async def ping():
    return {"ok": True, "ts": int(time.time())}

# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("server:app", host="0.0.0.0", port=port, log_level="info")
