# server.py
import argparse
import json
import os
import uvicorn
import asyncio
from bot import run_bot
from fastapi import FastAPI, WebSocket, Request, Form, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from twilio.rest import Client
from dotenv import load_dotenv
from loguru import logger
import db

load_dotenv(override=True)

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

templates = Jinja2Templates(directory="templates")

twilio_client = Client(
    os.getenv("TWILIO_ACCOUNT_SID"),
    os.getenv("TWILIO_AUTH_TOKEN")
)

call_data_store = {}

@app.on_event("startup")
async def startup_event():
    logger.info("Starting up Customer Care server...")

@app.get("/")
async def get_form(request: Request):
    """Serve the HTML form for customer support"""
    return templates.TemplateResponse(request=request, name="support_form.html")

@app.post("/send-otp")
async def send_otp(phone_no: str = Form(...)):
    """Send an OTP using Twilio Verify"""
    verify_sid = os.getenv("TWILIO_VERIFY_SERVICE_SID")
    if not verify_sid:
        return JSONResponse(status_code=500, content={"error": "Twilio Verify Service SID not configured."})
    
    try:
        verification = twilio_client.verify.v2.services(verify_sid).verifications.create(
            to=phone_no,
            channel="sms"
        )
        return {"status": "success", "message": f"OTP sent to {phone_no}"}
    except Exception as e:
        logger.error(f"Error sending OTP: {e}")
        return JSONResponse(status_code=400, content={"error": str(e)})

@app.post("/verify-otp")
async def verify_otp(phone_no: str = Form(...), otp_code: str = Form(...)):
    """Verify an OTP using Twilio Verify"""
    verify_sid = os.getenv("TWILIO_VERIFY_SERVICE_SID")
    try:
        verification_check = twilio_client.verify.v2.services(verify_sid).verification_checks.create(
            to=phone_no,
            code=otp_code
        )
        if verification_check.status == "approved":
            return {"status": "success"}
        return JSONResponse(status_code=400, content={"error": "Invalid OTP"})
    except Exception as e:
        logger.error(f"Error verifying OTP: {e}")
        return JSONResponse(status_code=400, content={"error": str(e)})

@app.post("/request-callback")
async def request_callback(
    name: str = Form(...),
    phone_no: str = Form(...),
    issue_type: str = Form(...)
):
    """Save request to DB and initiate immediate callback"""
    try:
        # 1. Save to DB
        request_id = db.create_support_request(name, phone_no, issue_type)
        logger.info(f"Saved support request #{request_id} for {name}")

        # 2. Initiate Call
        server_url = os.getenv("SERVER_URL", "")
        if not server_url:
            raise ValueError("SERVER_URL environment variable not set.")
        
        twiml_url = f"{server_url}/twiml"
        
        call = twilio_client.calls.create(
            to=phone_no,
            from_=os.getenv("TWILIO_PHONE_NUMBER"),
            url=twiml_url,
            method="POST"
        )
        
        logger.info(f"Outbound customer care call initiated: {call.sid} to {phone_no}")
        
        # Store data needed by the bot
        call_data_store[call.sid] = {
            "customer_name": name,
            "issue_type": issue_type
        }
        
        return {"status": "success", "message": "Callback initiated. You will receive a call shortly."}
        
    except Exception as e:
        logger.error(f"Error initiating callback: {str(e)}")
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.post("/twiml")
async def get_twiml(request: Request):
    """Return TwiML for outbound calls"""
    call_sid = (await request.form()).get("CallSid")
    logger.info(f"Serving TwiML for outbound call, CallSid: {call_sid}")
    
    server_url = os.getenv("SERVER_URL", "")
    websocket_url = server_url.replace("https://", "wss://").replace("http://", "ws://")
    
    twiml_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream url="{websocket_url}/ws"></Stream>
    </Connect>
    <Pause length="40"/>
</Response>"""
    
    return HTMLResponse(content=twiml_content, media_type="application/xml")

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """Handle WebSocket connections from Twilio"""
    await websocket.accept()
    logger.info("WebSocket connection accepted.")
    
    call_sid = None
    try:
        await websocket.receive_text()
        start_message = await websocket.receive_text()
        
        start_data = json.loads(start_message)
        stream_sid = start_data["start"]["streamSid"]
        call_sid = start_data["start"]["callSid"] 
        
        call_info = call_data_store.pop(call_sid, None)
        if not call_info:
            logger.error(f"No call data found for CallSid: {call_sid}. Closing WebSocket.")
            await websocket.close(code=1008)
            return

        customer_name = call_info.get("customer_name", "Customer")
        issue_type = call_info.get("issue_type", "General")
        
        await run_bot(
            websocket,
            stream_sid,
            app.state.testing,
            customer_name,
            issue_type,
            call_sid=call_sid,
            account_sid=os.getenv("TWILIO_ACCOUNT_SID"),
            auth_token=os.getenv("TWILIO_AUTH_TOKEN"),
        )
        
    except Exception as e:
        logger.error(f"Error in WebSocket endpoint for CallSid {call_sid}: {str(e)}")
        await websocket.close(code=1011)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Pipecat Twilio Outbound Call Server")
    parser.add_argument("-t", "--test", action="store_true", default=False)
    args, _ = parser.parse_known_args()
    
    app.state.testing = args.test
    
    required_vars = [
        "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_NUMBER",
        "SERVER_URL", "GROQ_API_KEY", "DEEPGRAM_API_KEY", "CARTESIA_API_KEY", "CARTESIA_VOICE_ID"
    ]
    
    missing_vars = [var for var in required_vars if not os.getenv(var)]
    if missing_vars:
        logger.error(f"Missing required environment variables: {missing_vars}")
        exit(1)
    
    uvicorn.run(app, host="0.0.0.0", port=8765)
