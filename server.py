# server.py
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

import argparse
import json
import os
import uvicorn
import asyncio
from datetime import datetime, timedelta
from bot import run_bot
from fastapi import FastAPI, WebSocket, Form, Request, UploadFile, File, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from twilio.rest import Client
from dotenv import load_dotenv
from loguru import logger
import aiofiles
import pypdf # New import for PDF parsing

load_dotenv(override=True)

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allow all origins for testing
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Initialize Jinja2 templates
templates = Jinja2Templates(directory="templates")

# Initialize Twilio client
twilio_client = Client(
    os.getenv("TWILIO_ACCOUNT_SID"),
    os.getenv("TWILIO_AUTH_TOKEN")
)

# In-memory storage for scheduled calls and call-specific data
# IMPORTANT: This data will be lost if the server restarts.
# For production, consider using a database (e.g., SQLite, PostgreSQL).
scheduled_calls = []
call_data_store = {} # To store data needed by the bot for a specific call_sid/stream_sid

RESUME_UPLOAD_DIR = "resumes"
os.makedirs(RESUME_UPLOAD_DIR, exist_ok=True)

@app.on_event("startup")
async def startup_event():
    logger.info("Starting up server...")
    # Start the background task for scheduling calls
    asyncio.create_task(call_scheduler())

@app.get("/")
async def get_form(request: Request):
    """Serve the HTML form for making outbound calls"""
    return templates.TemplateResponse(request=request, name="call_form.html")

@app.post("/schedule-interview")
async def schedule_interview(
    background_tasks: BackgroundTasks,
    candidate_name: str = Form(...),
    role: str = Form(...),
    phone_number: str = Form(...),
    schedule_date: str = Form(...), # YYYY-MM-DD
    schedule_time: str = Form(...), # HH:MM
    resume: UploadFile = File(...), # New parameter for file upload
):
    """Handle form submission, save resume, and schedule the outbound call"""
    try:
        # 1. Save the resume
        resume_filepath = os.path.join(RESUME_UPLOAD_DIR, resume.filename)
        async with aiofiles.open(resume_filepath, "wb") as out_file:
            while content := await resume.read(1024):  # read in chunks
                await out_file.write(content)
        
        # 2. Read resume content for the bot (now supports .txt and .pdf)
        resume_text = ""
        if resume.filename.endswith(".txt"):
            async with aiofiles.open(resume_filepath, "r") as f:
                resume_text = await f.read()
        elif resume.filename.endswith(".pdf"):
            try:
                # PyPDF2 requires a file-like object or path
                # For async, we need to read the file content into memory first
                # Or use a sync function in a thread pool executor if file is large
                with open(resume_filepath, "rb") as f:
                    reader = pypdf.PdfReader(f)
                    for page in reader.pages:
                        resume_text += page.extract_text() + "\n"
                logger.info(f"Successfully extracted text from PDF: {resume.filename}")
            except Exception as pdf_e:
                logger.error(f"Error extracting text from PDF {resume.filename}: {pdf_e}")
                resume_text = "Could not extract text from PDF. Bot will proceed without resume content."
        else:
            logger.warning(f"Unsupported resume file type: {resume.filename}. Bot will not use resume content.")

        # 3. Parse scheduled datetime
        scheduled_datetime_str = f"{schedule_date} {schedule_time}"
        scheduled_datetime = datetime.strptime(scheduled_datetime_str, "%Y-%m-%d %H:%M")
        
        # 4. Add call to scheduling queue
        scheduled_calls.append({
            "candidate_name": candidate_name,
            "role": role,
            "phone_number": phone_number,
            "scheduled_datetime": scheduled_datetime,
            "resume_text": resume_text, # Pass resume content
            "call_initiated": False # Flag to prevent duplicate calls
        })
        
        logger.info(f"Interview scheduled for {candidate_name} ({role}) on {scheduled_datetime_str}. Resume saved to {resume_filepath}")
        
        return HTMLResponse(content=f"""
        <html>
            <body>
                <h2>Interview Scheduled Successfully!</h2>
                <p>Candidate: {candidate_name}</p>
                <p>Role: {role}</p>
                <p>Phone Number: {phone_number}</p>
                <p>Scheduled For: {scheduled_datetime_str}</p>
                <p>Resume: {resume.filename}</p>
                <br>
                <a href="/">Schedule Another Interview</a>
            </body>
        </html>
        """)
        
    except Exception as e:
        logger.error(f"Error scheduling interview: {str(e)}")
        return HTMLResponse(content=f"""
        <html>
            <body>
                <h2>Error Scheduling Interview</h2>
                <p>Error: {str(e)}</p>
                <br>
                <a href="/">Try Again</a>
            </body>
        </html>
        """, status_code=500)

async def call_scheduler():
    """Background task to initiate scheduled calls"""
    while True:
        now = datetime.now()
        calls_to_initiate = []

        # Find calls that are due and haven't been initiated yet
        for call_info in scheduled_calls:
            if not call_info["call_initiated"] and now >= call_info["scheduled_datetime"]:
                calls_to_initiate.append(call_info)
                call_info["call_initiated"] = True # Mark as initiated

        for call_info in calls_to_initiate:
            try:
                server_url = os.getenv("SERVER_URL", "") # This should be your public ngrok/domain URL
                if not server_url:
                    logger.error("SERVER_URL environment variable not set. Cannot make outbound calls.")
                    continue

                twiml_url = f"{server_url}/twiml"
                
                call = twilio_client.calls.create(
                    to=call_info["phone_number"],
                    from_=os.getenv("TWILIO_PHONE_NUMBER"),
                    url=twiml_url,
                    method="POST"
                )
                
                logger.info(f"Outbound interview call initiated for {call_info['candidate_name']} ({call_info['role']}): {call.sid} to {call_info['phone_number']}")
                
                # Store data needed by the bot, indexed by Twilio Call SID
                call_data_store[call.sid] = {
                    "candidate_name": call_info["candidate_name"],
                    "role": call_info["role"],
                    "resume_text": call_info["resume_text"]
                }

            except Exception as e:
                logger.error(f"Error initiating scheduled call for {call_info['candidate_name']}: {str(e)}")
        
        # Check every 60 seconds for new calls to initiate
        await asyncio.sleep(60)

@app.post("/twiml")
async def get_twiml(request: Request):
    """Return TwiML for outbound calls"""
    # Twilio sends form data, so we access CallSid from request.form()
    call_sid = (await request.form()).get("CallSid")
    logger.info(f"Serving TwiML for outbound call, CallSid: {call_sid}")
    
    server_url = os.getenv("SERVER_URL", "")
    if not server_url:
        raise ValueError("SERVER_URL environment variable not set.")

    websocket_url = server_url.replace("https://", "wss://").replace("http://", "ws://")
    
    # The CallSid will be passed in the initial WebSocket 'start' message, not the URL
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
    
    # Initialize call_sid to None for error logging in case it's not found
    call_sid = None
    try:
        # Consume the first message (usually 'connected' event from Twilio)
        await websocket.receive_text()

        # The second message is the "start" event with streamSid and callSid
        start_message = await websocket.receive_text()
        
        start_data = json.loads(start_message)
        logger.info(f"Call data received: {start_data}")
        
        stream_sid = start_data["start"]["streamSid"]
        # Get call_sid directly from the start_data dictionary
        call_sid = start_data["start"]["callSid"] 
        logger.info(f"Stream SID: {stream_sid} for CallSid: {call_sid}")
        
        # Retrieve data for this specific call_sid from the in-memory store
        call_info = call_data_store.pop(call_sid, None) # Pop to remove after use
        if not call_info:
            logger.error(f"No call data found for CallSid: {call_sid}. Closing WebSocket.")
            await websocket.close(code=1008) # Policy Violation
            return

        candidate_name = call_info.get("candidate_name", "candidate")
        role = call_info.get("role", "general position")
        resume_text = call_info.get("resume_text", "No resume provided.")
        
        # Run the bot with interview-specific data
        await run_bot(
            websocket,
            stream_sid,
            app.state.testing,
            candidate_name,
            role,
            resume_text,
            call_sid=call_sid,
            account_sid=os.getenv("TWILIO_ACCOUNT_SID"),
            auth_token=os.getenv("TWILIO_AUTH_TOKEN"),
        )
        
    except Exception as e:
        logger.error(f"Error in WebSocket endpoint for CallSid {call_sid}: {str(e)}")
        await websocket.close(code=1011) # Internal Error

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Pipecat Twilio Outbound Call Server")
    parser.add_argument(
        "-t", "--test", action="store_true", default=False, help="set the server in testing mode"
    )
    args, _ = parser.parse_known_args()
    
    app.state.testing = args.test
    
    # Check required environment variables
    required_vars = [
        "TWILIO_ACCOUNT_SID",
        "TWILIO_AUTH_TOKEN", 
        "TWILIO_PHONE_NUMBER",
        "SERVER_URL", # This MUST be set to your public ngrok URL or domain
        "GROQ_API_KEY", # Required by bot.py
        "DEEPGRAM_API_KEY", # Required by bot.py
        "ELEVEN_API_KEY", # Required by bot.py
        "ELEVEN_VOICE_ID", # Required by bot.py
    ]
    
    missing_vars = [var for var in required_vars if not os.getenv(var)]
    if missing_vars:
        logger.error(f"Missing required environment variables: {missing_vars}")
        exit(1)
    
    uvicorn.run(app, host="0.0.0.0", port=8765)

