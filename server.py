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
from pathlib import Path
from contextlib import asynccontextmanager

import httpx
import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from telethon import TelegramClient, events
from telethon.sessions import StringSession

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

def get_config():
    return {
        "TG_API_ID":       os.environ["TG_API_ID"],
        "TG_API_HASH":     os.environ["TG_API_HASH"],
        "TG_PHONE":        os.environ.get("TG_PHONE", ""),
        "TG_SESSION":      os.environ.get("TG_SESSION", ""),
        "WTS_BOT":         os.environ.get("WTS_BOT_USERNAME", "@WStaskbot"),
        "WTR_BOT":         os.environ.get("WTR_BOT_USERNAME", "@wsotp200bot"),
        "CF_URL":          os.environ["CF_WORKER_URL"].rstrip("/"),
        "INTERNAL_SECRET": os.environ["INTERNAL_SECRET"],
    }

config = get_config()

# ─────────────────────────────────────────────
# TELETHON CLIENT
# ─────────────────────────────────────────────

tg_client = TelegramClient(
    StringSession(config["TG_SESSION"]),
    int(config["TG_API_ID"]),
    config["TG_API_HASH"]
)

WTS_BOT = config["WTS_BOT"]
WTR_BOT = config["WTR_BOT"]
CF_URL  = config["CF_URL"]
SECRET  = config["INTERNAL_SECRET"]

# ─────────────────────────────────────────────
# WTR IN-MEMORY TRACKING
# {number: {progress_count, progress1_msg_id, progress2_msg_id,
#           status, otp_submitted, registration_id, timeout_task}}
# ─────────────────────────────────────────────

wtr_tracking: dict = {}
wtr_lock = asyncio.Lock()
OTP_TIMEOUT = 360  # 6 minutes

# ─────────────────────────────────────────────
# CF CALLER
# ─────────────────────────────────────────────

async def call_cf(path: str, body: dict, *, is_wtr_callback: bool = False):
    """POST to CF worker. WTR callbacks use HMAC signature."""
    try:
        headers = {"Content-Type": "application/json"}
        if is_wtr_callback:
            payload = json.dumps(body, separators=(",", ":"), sort_keys=True)
            sig = hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
            headers["X-WTR-Signature"] = sig
        else:
            headers["X-Internal-Secret"] = SECRET
            payload = json.dumps(body)

        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(f"{CF_URL}{path}", content=payload, headers=headers)
            print(f"CF {path} → {r.status_code}")
            return r.json()
    except Exception as e:
        print(f"CF call failed {path}: {e}")
        return None

# ─────────────────────────────────────────────
# WTS MESSAGE PARSERS
# ─────────────────────────────────────────────

def wts_parse_task_completion(text: str):
    if "Task Completed" not in text:
        return None
    number = re.search(r'\b(880\d{9,}|\d{10,13})\b', text)
    total  = re.search(r'Total successfully sent:\s*(\d+)', text)
    if number and total:
        return number.group(1), int(total.group(1))
    return None

def wts_parse_auth_failed(text: str):
    if "Authorization failed" not in text:
        return None
    number = re.search(r'\b(880\d{9,}|\d{10,13})\b', text)
    return number.group(1) if number else None

def wts_parse_login_success(text: str):
    if "logged in successfully" not in text.lower():
        return None
    number = re.search(r'Number:\s*(\d+)', text)
    return number.group(1) if number else None

def wts_parse_pairing_code(text: str):
    match = re.search(r'Pairing code:.*?(\d{4}-\d{4})', text)
    return match.group(1) if match else None

# ─────────────────────────────────────────────
# WTR MESSAGE PARSERS
# ─────────────────────────────────────────────

def wtr_extract_number(text: str):
    """Phone number at start of bot message."""
    if not text:
        return None
    m = re.match(r'^(\d{7,15})', text.strip())
    return m.group(1) if m else None

def wtr_classify(text: str):
    if not text:
        return None
    t = text.strip()
    if "🔵" in t and "In Progress" in t:
        return "progress"
    if "🟢" in t and "Success" in t:
        return "success"
    if "💰" in t and "New Reward Notification" in t:
        return "reward"
    if "please submit this number again in" in t.lower():
        return "retry_later"
    if "verification code can only be 6 digits" in t.lower():
        return "wrong_otp_format"
    if "this number is wrong" in t.lower() or "country code is not supported" in t.lower():
        return "invalid_number"
    if "🟡" in t:
        return "try_later"
    return None

# ─────────────────────────────────────────────
# WTR TIMEOUT HANDLER
# ─────────────────────────────────────────────

async def wtr_timeout(number: str):
    await asyncio.sleep(OTP_TIMEOUT)
    async with wtr_lock:
        entry = wtr_tracking.get(number)
        if not entry or entry["status"] in ("success", "failed"):
            return
        reg_id = entry.get("registration_id")
        entry["status"] = "timeout"
    await call_cf("/wtr/callback",
                  {"number": number, "registration_id": reg_id, "event": "timeout"},
                  is_wtr_callback=True)
    async with wtr_lock:
        wtr_tracking.pop(number, None)

# ─────────────────────────────────────────────
# TELEGRAM EVENT HANDLERS
# ─────────────────────────────────────────────

async def get_sender_username(event):
    try:
        sender = await event.get_sender()
        return (getattr(sender, "username", "") or "").lower().lstrip("@")
    except Exception:
        return ""

@tg_client.on(events.NewMessage())
async def on_any_message(event):
    username = await get_sender_username(event)
    text     = event.message.message or ""
    msg_id   = event.message.id

    # ── WTS BOT ──
    if username == WTS_BOT.lower().lstrip("@"):
        print(f"[WTS] {text[:200]}")

        result = wts_parse_task_completion(text)
        if result:
            number, total_sent = result
            await call_cf("/tg-event", {
                "type": "task_complete",
                "data": {"whatsapp_number": number, "total_sent": total_sent, "raw_message": text}
            })
            return

        number = wts_parse_auth_failed(text)
        if number:
            await call_cf("/tg-event", {
                "type": "number_removed",
                "data": {"whatsapp_number": number}
            })
        return

    # ── WTR BOT ──
    if username == WTR_BOT.lower().lstrip("@"):
        print(f"[WTR] msg_id={msg_id} | {text[:200]}")
        event_type = wtr_classify(text)
        if not event_type:
            return

        # Reward message — extract number from body
        if event_type == "reward":
            m = re.search(r"Number:\s*(\d{7,15})", text)
            number = m.group(1) if m else None
            if not number:
                return
            async with wtr_lock:
                entry  = wtr_tracking.get(number)
                reg_id = entry.get("registration_id") if entry else None
                if entry:
                    entry["status"] = "success"
                    if entry.get("timeout_task"):
                        entry["timeout_task"].cancel()
            await call_cf("/wtr/callback",
                          {"number": number, "registration_id": reg_id, "event": "success"},
                          is_wtr_callback=True)
            async with wtr_lock:
                wtr_tracking.pop(number, None)
            return

        number = wtr_extract_number(text)
        if not number:
            return

        async with wtr_lock:
            entry = wtr_tracking.get(number)
            if not entry:
                return
            reg_id = entry.get("registration_id")

            if event_type == "progress":
                entry["progress_count"] += 1
                count = entry["progress_count"]
                if count == 1:
                    entry["progress1_msg_id"] = msg_id
                    entry["status"] = "progress1"
                    # Don't callback yet — wait for progress2 or try_later

                elif count == 2:
                    entry["progress2_msg_id"] = msg_id
                    entry["status"] = "progress2"
                    if entry.get("timeout_task"):
                        entry["timeout_task"].cancel()
                    entry["timeout_task"] = asyncio.create_task(wtr_timeout(number))
                    # Need OTP from user
                    asyncio.create_task(call_cf(
                        "/wtr/callback",
                        {"number": number, "registration_id": reg_id, "event": "needs_otp"},
                        is_wtr_callback=True
                    ))

            elif event_type == "try_later":
                entry["status"] = "failed"
                if entry.get("timeout_task"):
                    entry["timeout_task"].cancel()
                reason = "wrong_otp" if entry.get("otp_submitted") else "try_later"
                asyncio.create_task(call_cf(
                    "/wtr/callback",
                    {"number": number, "registration_id": reg_id, "event": "failed", "reason": reason},
                    is_wtr_callback=True
                ))
                wtr_tracking.pop(number, None)

            elif event_type == "retry_later":
                m = re.search(r"(\d+)\s*second", text, re.IGNORECASE)
                wait = int(m.group(1)) if m else 6
                asyncio.create_task(call_cf(
                    "/wtr/callback",
                    {"number": number, "registration_id": reg_id, "event": "retry_later", "wait_seconds": wait},
                    is_wtr_callback=True
                ))

            elif event_type == "wrong_otp_format":
                asyncio.create_task(call_cf(
                    "/wtr/callback",
                    {"number": number, "registration_id": reg_id, "event": "wrong_otp_format"},
                    is_wtr_callback=True
                ))

            elif event_type == "invalid_number":
                entry["status"] = "failed"
                if entry.get("timeout_task"):
                    entry["timeout_task"].cancel()
                asyncio.create_task(call_cf(
                    "/wtr/callback",
                    {"number": number, "registration_id": reg_id, "event": "invalid_number"},
                    is_wtr_callback=True
                ))
                wtr_tracking.pop(number, None)

# ─────────────────────────────────────────────
# FASTAPI APP
# ─────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("Starting Telethon...")
    await tg_client.start(phone=config.get("TG_PHONE") or None)
    print(f"Telethon connected | WTS={WTS_BOT} WTR={WTR_BOT}")
    yield
    await tg_client.disconnect()

app = FastAPI(title="WTS+WTR Server", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ─────────────────────────────────────────────
# SECURITY
# ─────────────────────────────────────────────

def verify_internal(req: Request):
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
    def sse(event: str, data: str):
        return f"event: {event}\ndata: {data}\n\n"

    try:
        bot = await tg_client.get_entity(WTS_BOT)

        yield sse("status", "Sending number to bot...")
        await tg_client.send_message(bot, whatsapp_number)
        await asyncio.sleep(4)

        msgs = await tg_client.get_messages(bot, limit=1)
        msg  = msgs[0]
        if not msg.buttons:
            yield sse("error", "Bot did not respond with buttons.")
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

        await asyncio.sleep(4)

        msgs = await tg_client.get_messages(bot, limit=1)
        msg  = msgs[0]
        if not msg.buttons:
            yield sse("error", "Bot did not respond with limit buttons.")
            return

        yield sse("status", f"Selecting limit {limit}...")
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
            yield sse("error", f"Could not find limit '{limit}'.")
            return

        await asyncio.sleep(4)

        yield sse("status", "Waiting for pairing code...")
        pairing_code = None
        for _ in range(10):
            msgs = await tg_client.get_messages(bot, limit=1)
            code = wts_parse_pairing_code(msgs[0].message or "")
            if code:
                pairing_code = code
                break
            await asyncio.sleep(3)

        if not pairing_code:
            yield sse("error", "Pairing code not received. Try again.")
            return

        daily_limit = int(limit) if limit != "NoLimit" else 999
        await call_cf("/tg-event", {
            "type": "number_pending",
            "data": {"user_id": user_id, "whatsapp_number": whatsapp_number,
                     "account_type": account_type, "daily_limit": daily_limit}
        })

        yield sse("pairing_code", pairing_code)
        yield sse("status", "Enter code in WhatsApp → Linked Devices. Waiting (4 min max)...")

        for _ in range(48):
            msgs = await tg_client.get_messages(bot, limit=1)
            text = msgs[0].message or ""
            success_number = wts_parse_login_success(text)
            if success_number and success_number == whatsapp_number:
                await call_cf("/tg-event", {
                    "type": "number_added",
                    "data": {"user_id": user_id, "whatsapp_number": whatsapp_number,
                             "account_type": account_type, "daily_limit": daily_limit}
                })
                yield sse("success", "Account added successfully ✅")
                return
            await asyncio.sleep(5)

        yield sse("error", "Timed out. Code expired. Please try again.")

    except Exception as e:
        yield sse("error", f"Error: {str(e)}")

@app.post("/add-number")
async def add_number(req: AddNumberReq):
    return StreamingResponse(
        add_number_flow(req.whatsapp_number, req.account_type, req.limit, req.user_id),
        media_type="text/event-stream"
    )

# ─────────────────────────────────────────────
# WTR — SEND NUMBER
# ─────────────────────────────────────────────

class SendNumberReq(BaseModel):
    number: str
    registration_id: str

@app.post("/wtr/send-number")
async def wtr_send_number(req: SendNumberReq, request: Request):
    verify_internal(request)
    number = req.number.strip().lstrip("+")
    if not number.isdigit() or len(number) < 7:
        raise HTTPException(400, "Invalid number")

    async with wtr_lock:
        if number in wtr_tracking:
            raise HTTPException(409, "Number already being tracked")
        wtr_tracking[number] = {
            "progress_count":   0,
            "progress1_msg_id": None,
            "progress2_msg_id": None,
            "status":           "waiting",
            "otp_submitted":    False,
            "registration_id":  req.registration_id,
            "timeout_task":     None,
        }

    try:
        bot = await tg_client.get_entity(WTR_BOT)
        await tg_client.send_message(bot, number)
        print(f"[WTR] Sent {number} to bot")
    except Exception as e:
        async with wtr_lock:
            wtr_tracking.pop(number, None)
        raise HTTPException(500, f"Failed to send: {e}")

    return {"status": "sent", "number": number}

# ─────────────────────────────────────────────
# WTR — SEND OTP
# ─────────────────────────────────────────────

class SendOtpReq(BaseModel):
    number: str
    otp: str
    registration_id: str

@app.post("/wtr/send-otp")
async def wtr_send_otp(req: SendOtpReq, request: Request):
    verify_internal(request)
    number = req.number.strip().lstrip("+")
    otp    = req.otp.strip()

    if not otp.isdigit() or len(otp) != 6:
        raise HTTPException(400, "OTP must be exactly 6 digits")

    async with wtr_lock:
        entry = wtr_tracking.get(number)
        if not entry:
            raise HTTPException(404, "Number not tracked")
        if entry["status"] != "progress2":
            raise HTTPException(409, f"Not awaiting OTP (status={entry['status']})")
        msg_id = entry["progress2_msg_id"]
        if not msg_id:
            raise HTTPException(500, "No progress2 message ID")
        entry["otp_submitted"] = True

    try:
        bot = await tg_client.get_entity(WTR_BOT)
        await tg_client.send_message(bot, otp, reply_to=msg_id)
        print(f"[WTR] OTP {otp} sent for {number} → msg {msg_id}")
    except Exception as e:
        raise HTTPException(500, f"Failed to send OTP: {e}")

    return {"status": "otp_sent"}

# ─────────────────────────────────────────────
# WTR — STATUS
# ─────────────────────────────────────────────

@app.get("/wtr/status/{number}")
async def wtr_status(number: str, request: Request):
    verify_internal(request)
    number = number.strip().lstrip("+")
    async with wtr_lock:
        entry = wtr_tracking.get(number)
    if not entry:
        return {"number": number, "status": "not_tracked"}
    return {
        "number":         number,
        "status":         entry["status"],
        "progress_count": entry["progress_count"],
        "has_otp_msg":    entry["progress2_msg_id"] is not None,
    }

# ─────────────────────────────────────────────
# HEALTH / PING
# ─────────────────────────────────────────────

@app.get("/health")
async def health():
    return {
        "status":           "ok",
        "tg_connected":     tg_client.is_connected(),
        "wtr_tracked":      len(wtr_tracking),
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
