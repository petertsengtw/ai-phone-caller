#
# ai-phone-caller - Open Source AI Phone Agent
# Built for OpenClaw (https://openclaw.ai)
#
# SPDX-License-Identifier: MIT
#

"""server.py - FastAPI server for outbound AI phone calls.

Exposes a REST API to trigger calls and handles WebSocket connections
from the telephony provider (Twilio/Telnyx).
"""

import os
import re
from contextlib import asynccontextmanager

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from loguru import logger
from pydantic import BaseModel
from twilio.rest import Client as TwilioClient
from twilio.twiml.voice_response import Connect, Stream, VoiceResponse

from languages import LANGUAGES, resolve_language

load_dotenv(override=True)


# --------------- Models --------------- #


class CallRequest(BaseModel):
    """Request to initiate an outbound call."""

    to: str  # Phone number to call (E.164)
    task: str = ""  # What the AI should do on this call
    caller_name: str = ""  # Who the AI represents
    greeting: str = ""  # Optional first thing to say
    language: str = ""  # de, en, or zh-TW (defaults to CALLER_LANGUAGE)
    extension: str = ""  # Keys to press after pickup, e.g. "ww123#" (each w = wait 0.5s)
    ivr: bool = True  # Listen first and let the AI navigate phone menus


class CallResponse(BaseModel):
    """Response after initiating a call."""

    call_sid: str
    status: str
    to: str


# --------------- Twilio Helpers --------------- #


def get_twilio_client() -> TwilioClient:
    sid = os.getenv("TWILIO_ACCOUNT_SID")
    token = os.getenv("TWILIO_AUTH_TOKEN")
    if not sid or not token:
        raise ValueError("TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN required")
    return TwilioClient(sid, token)


def get_ws_url() -> str:
    """Get WebSocket URL for Twilio to connect back to us."""
    server_url = os.getenv("LOCAL_SERVER_URL", "")
    if not server_url:
        raise ValueError("LOCAL_SERVER_URL required (your ngrok/public URL)")
    return server_url.replace("https://", "wss://").replace("http://", "ws://") + "/ws"


# --------------- Active Calls Store --------------- #

# In-memory store for call configs (call_sid -> config)
_call_configs: dict[str, dict] = {}


# --------------- API --------------- #


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("AI Phone Caller server starting")
    yield
    logger.info("AI Phone Caller server stopping")


app = FastAPI(
    title="AI Phone Caller",
    description="Open Source AI-powered outbound phone calls via Pipecat",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health():
    return {"status": "ok", "version": "0.1.0"}


@app.post("/call", response_model=CallResponse)
async def initiate_call(req: CallRequest):
    """Start an outbound phone call.

    The AI agent will call the specified number and execute the task.

    Example:
        POST /call
        {
            "to": "+4930123456",
            "task": "Make an appointment for next Tuesday at 2pm for a haircut",
            "caller_name": "Max Mustermann"
        }
    """
    logger.info(f"Outbound call request: {req.to}")

    try:
        language = resolve_language(req.language)
    except ValueError as e:
        raise HTTPException(400, str(e))

    if not re.fullmatch(r"[0-9*#wW]*", req.extension):
        raise HTTPException(400, "extension may only contain 0-9, *, #, and w/W (wait)")

    from_number = os.getenv("TWILIO_PHONE_NUMBER")
    if not from_number:
        raise HTTPException(500, "TWILIO_PHONE_NUMBER not configured")

    server_url = os.getenv("LOCAL_SERVER_URL")
    if not server_url:
        raise HTTPException(500, "LOCAL_SERVER_URL not configured")

    twiml_url = f"{server_url}/twiml"

    call_options = {}
    if req.extension:
        # Twilio presses these keys right after the call is answered
        call_options["send_digits"] = req.extension

    try:
        client = get_twilio_client()
        call = client.calls.create(
            to=req.to,
            from_=from_number,
            url=twiml_url,
            method="POST",
            status_callback=f"{server_url}/call-status",
            status_callback_event=["initiated", "ringing", "answered", "completed"],
            **call_options,
        )

        # Store call config for when WebSocket connects
        _call_configs[call.sid] = {
            "task": req.task,
            "caller_name": req.caller_name,
            "greeting": req.greeting,
            "language": language,
            "ivr": req.ivr,
            "to": req.to,
        }

        logger.info(f"Call initiated: {call.sid}")
        return CallResponse(call_sid=call.sid, status="initiated", to=req.to)

    except Exception as e:
        logger.error(f"Failed to initiate call: {e}")
        raise HTTPException(500, f"Call failed: {str(e)}")


@app.post("/twiml")
async def serve_twiml(request: Request):
    """Return TwiML that connects the call to our WebSocket."""
    form = await request.form()
    to_number = form.get("To", "")
    from_number = form.get("From", "")
    call_sid = form.get("CallSid", "")

    logger.info(f"TwiML request for call {call_sid}: {from_number} -> {to_number}")

    ws_url = get_ws_url()

    response = VoiceResponse()
    connect = Connect()
    stream = Stream(url=ws_url)

    # Pass metadata to bot via stream parameters
    stream.parameter(name="to_number", value=to_number)
    stream.parameter(name="from_number", value=from_number)
    stream.parameter(name="call_sid", value=call_sid)

    # Pass task config if we have it
    config = _call_configs.get(call_sid, {})
    if config.get("task"):
        stream.parameter(name="task", value=config["task"])
    if config.get("caller_name"):
        stream.parameter(name="caller_name", value=config["caller_name"])
    if config.get("language"):
        stream.parameter(name="language", value=config["language"])
    stream.parameter(name="ivr", value="1" if config.get("ivr", True) else "0")

    connect.append(stream)
    response.append(connect)
    response.pause(length=120)  # Keep call alive up to 2 min

    return HTMLResponse(content=str(response), media_type="application/xml")


@app.post("/inbound-twiml")
async def serve_inbound_twiml(request: Request):
    """Return TwiML for inbound calls (someone calling our number)."""
    form = await request.form()
    to_number = form.get("To", "")
    from_number = form.get("From", "")
    call_sid = form.get("CallSid", "")

    logger.info(f"INBOUND call {call_sid}: {from_number} -> {to_number}")

    ws_url = get_ws_url()

    # Store config for inbound call - bot will be Botty, an AI assistant
    language = resolve_language()
    _call_configs[call_sid] = {
        "task": LANGUAGES[language]["inbound_task"],
        "caller_name": "Botty",
        "language": language,
    }

    response = VoiceResponse()
    connect = Connect()
    stream = Stream(url=ws_url)

    stream.parameter(name="to_number", value=to_number)
    stream.parameter(name="from_number", value=from_number)
    stream.parameter(name="call_sid", value=call_sid)
    stream.parameter(name="task", value=_call_configs[call_sid]["task"])
    stream.parameter(name="caller_name", value="Botty")
    stream.parameter(name="language", value=language)
    stream.parameter(name="ivr", value="0")  # Inbound: a human called us, greet right away

    connect.append(stream)
    response.append(connect)
    response.pause(length=120)

    return HTMLResponse(content=str(response), media_type="application/xml")


@app.post("/call-status")
async def call_status(request: Request):
    """Receive call status updates from Twilio."""
    form = await request.form()
    call_sid = form.get("CallSid", "")
    status = form.get("CallStatus", "")
    duration = form.get("CallDuration", "0")

    logger.info(f"Call {call_sid}: {status} (duration: {duration}s)")

    # Clean up config when call completes
    if status in ("completed", "failed", "busy", "no-answer", "canceled"):
        _call_configs.pop(call_sid, None)

    return JSONResponse({"received": True})


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """Handle WebSocket from Twilio Media Streams -> run Pipecat bot."""
    from bot import bot
    from pipecat.runner.types import WebSocketRunnerArguments

    await websocket.accept()
    logger.info("WebSocket connected")

    try:
        runner_args = WebSocketRunnerArguments(websocket=websocket)
        runner_args.handle_sigint = False
        await bot(runner_args)
    except Exception as e:
        logger.error(f"Bot error: {e}")
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


# --------------- Main --------------- #


if __name__ == "__main__":
    port = int(os.getenv("PORT", "7860"))
    logger.info(f"Starting AI Phone Caller on port {port}")
    uvicorn.run(app, host="0.0.0.0", port=port)
