# server.py
import argparse
import json
import os
import uvicorn
import asyncio
from contextlib import asynccontextmanager
from bot import run_bot
from fastapi import FastAPI, WebSocket, Request, Form, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv
from loguru import logger
import aiohttp
import db

load_dotenv(override=True)

# telnyx client not needed since we only use HTTP POST for TeXML calls
call_data_store = {}

templates = Jinja2Templates(directory="templates")


# ------------- HELPERS ------------- #

async def make_telnyx_call(
    session: aiohttp.ClientSession, to_number: str, from_number: str, texml_url: str
):
    """Make an outbound call using Telnyx's TeXML API."""
    api_key = os.getenv("TELNYX_API_KEY")
    account_sid = os.getenv("TELNYX_ACCOUNT_SID")
    application_sid = os.getenv("TELNYX_APPLICATION_SID")

    if not api_key:
        raise ValueError("Missing Telnyx API key (TELNYX_API_KEY)")
    if not account_sid:
        raise ValueError("Missing Telnyx Account SID (TELNYX_ACCOUNT_SID)")
    if not application_sid:
        raise ValueError("Missing Telnyx TeXML Application SID (TELNYX_APPLICATION_SID)")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    data = {
        "ApplicationSid": application_sid,
        "To": to_number,
        "From": from_number,
        "Url": texml_url,
    }

    url = f"https://api.telnyx.com/v2/texml/Accounts/{account_sid}/Calls"

    async with session.post(url, headers=headers, json=data) as response:
        if response.status != 200:
            error_text = await response.text()
            raise Exception(f"Telnyx API error ({response.status}): {error_text}")

        result = await response.json()
        return result


# ------------- APP LIFECYCLE ------------- #

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Create aiohttp session for Telnyx API calls on startup, close on shutdown."""
    app.state.session = aiohttp.ClientSession()
    logger.info("Starting up Customer Care server...")
    yield
    await app.state.session.close()


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ------------- ROUTES ------------- #

@app.get("/")
async def get_form(request: Request):
    """Serve the HTML form for customer support"""
    return templates.TemplateResponse(request=request, name="support_form.html")



@app.post("/request-callback")
async def request_callback(
    name: str = Form(...),
    phone_no: str = Form(...),
    issue_type: str = Form(...)
):
    """Save request to DB and initiate immediate callback via Telnyx"""
    try:
        # 1. Save to DB
        request_id = db.create_support_request(name, phone_no, issue_type)
        logger.info(f"Saved support request #{request_id} for {name}")

        # 2. Initiate Call via Telnyx TeXML API
        server_url = os.getenv("SERVER_URL", "")
        if not server_url:
            raise ValueError("SERVER_URL environment variable not set.")

        texml_url = f"{server_url}/texml"

        result = await make_telnyx_call(
            session=app.state.session,
            to_number=phone_no,
            from_number=os.getenv("TELNYX_PHONE_NUMBER"),
            texml_url=texml_url,
        )

        call_sid = result.get("sid") or result.get("call_sid", "unknown")
        logger.info(f"Outbound customer care call initiated: {call_sid} to {phone_no}")

        # Store data needed by the bot, keyed by call SID
        call_data_store[call_sid] = {
            "customer_name": name,
            "issue_type": issue_type
        }

        return {"status": "success", "message": "Callback initiated. You will receive a call shortly."}

    except Exception as e:
        logger.error(f"Error initiating callback: {str(e)}")
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/texml")
async def get_texml(request: Request):
    """Return TeXML for outbound calls (Telnyx equivalent of TwiML)"""
    form_data = await request.form()
    call_sid = form_data.get("CallSid")
    logger.info(f"Serving TeXML for outbound call, CallSid: {call_sid}")

    server_url = os.getenv("SERVER_URL", "")
    websocket_url = server_url.replace("https://", "wss://").replace("http://", "ws://")

    # TeXML uses the same XML format as TwiML — the <Stream> verb is compatible
    texml_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream url="{websocket_url}/ws"></Stream>
    </Connect>
    <Pause length="40"/>
</Response>"""

    return HTMLResponse(content=texml_content, media_type="application/xml")


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """Handle WebSocket connections from Telnyx media streams"""
    await websocket.accept()
    logger.info("WebSocket connection accepted.")

    call_control_id = None
    try:
        # Telnyx sends a connected event followed by a start event
        await websocket.receive_text()
        start_message = await websocket.receive_text()

        start_data = json.loads(start_message)

        # Telnyx TeXML streams use the same message format as Twilio streams
        # Extract stream ID and call SID from the start message
        stream_id = start_data["start"]["streamSid"]
        call_sid = start_data["start"]["callSid"]

        # Try to extract call_control_id if available in custom parameters
        call_control_id = start_data["start"].get("customParameters", {}).get("call_control_id", call_sid)

        call_info = call_data_store.pop(call_sid, None)
        if not call_info:
            logger.warning(f"No call data found for CallSid: {call_sid}. Using default values.")
            call_info = {"customer_name": "Customer", "issue_type": "General"}

        customer_name = call_info.get("customer_name", "Customer")
        issue_type = call_info.get("issue_type", "General")

        await run_bot(
            websocket,
            stream_id,
            app.state.testing,
            customer_name,
            issue_type,
            call_control_id=call_control_id,
            api_key=os.getenv("TELNYX_API_KEY"),
        )

    except Exception as e:
        logger.error(f"Error in WebSocket endpoint for call {call_control_id}: {str(e)}")
        await websocket.close(code=1011)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Pipecat Telnyx Outbound Call Server")
    parser.add_argument("-t", "--test", action="store_true", default=False)
    args, _ = parser.parse_known_args()

    app.state.testing = args.test

    required_vars = [
        "TELNYX_API_KEY", "TELNYX_ACCOUNT_SID", "TELNYX_APPLICATION_SID",
        "TELNYX_PHONE_NUMBER", "SERVER_URL",
        "GROQ_API_KEY", "DEEPGRAM_API_KEY", "CARTESIA_API_KEY", "CARTESIA_VOICE_ID"
    ]

    missing_vars = [var for var in required_vars if not os.getenv(var)]
    if missing_vars:
        logger.error(f"Missing required environment variables: {missing_vars}")
        exit(1)

    uvicorn.run(app, host="0.0.0.0", port=8765)
