# bot.py
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

import datetime
import io
import os
import sys
import wave
import json

import aiofiles
from dotenv import load_dotenv
from fastapi import WebSocket
from loguru import logger

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.frames.frames import LLMContextFrame
from pipecat.processors.audio.audio_buffer_processor import AudioBufferProcessor
from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.services.deepgram.stt import DeepgramSTTService
# from pipecat.services.google.llm import GoogleLLMService # Removed GoogleLLMService import
from pipecat.services.groq.llm import GroqLLMService # New import for GroqLLMService
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)

load_dotenv(override=True)

logger.remove(0)
logger.add(sys.stderr, level="DEBUG")

CONVERSATION_LOG_DIR = "conversations"
os.makedirs(CONVERSATION_LOG_DIR, exist_ok=True)

# Directory for storing audio recordings
RECORDINGS_DIR = "recordings"
os.makedirs(RECORDINGS_DIR, exist_ok=True)

async def save_audio(server_name: str, audio: bytes, sample_rate: int, num_channels: int):
    """
    Saves audio data to a WAV file in the 'recordings' directory.
    The filename includes the server name and a timestamp for uniqueness.
    """
    if len(audio) > 0:
        filename = os.path.join(
            RECORDINGS_DIR,
            f"{server_name}_recording_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.wav"
        )
        with io.BytesIO() as buffer:
            with wave.open(buffer, "wb") as wf:
                wf.setsampwidth(2) # 16-bit audio
                wf.setnchannels(num_channels)
                wf.setframerate(sample_rate)
                wf.writeframes(audio)
            async with aiofiles.open(filename, "wb") as file:
                await file.write(buffer.getvalue())
        logger.info(f"Merged audio saved to {filename}")
    else:
        logger.info("No audio data to save")


async def run_bot(websocket_client: WebSocket, stream_sid: str, testing: bool,
                  customer_name: str, issue_type: str,
                  call_sid: str = None, account_sid: str = None, auth_token: str = None):
    """
    Runs the AI interview bot, handling WebSocket communication, STT, LLM, and TTS.
    It also manages conversation logging and audio recording.
    """
    transport = FastAPIWebsocketTransport(
        websocket=websocket_client,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            add_wav_header=False, # Twilio doesn't expect WAV headers in stream
            vad_enabled=True,
            vad_analyzer=SileroVADAnalyzer(
                params=VADParams(
                    stop_secs=0.7,
                    min_volume=0.3,
                    confidence=0.5,
                )
            ),
            vad_audio_passthrough=True, # Pass VAD audio to STT
            serializer=TwilioFrameSerializer(
                stream_sid,
                call_sid=call_sid,
                account_sid=account_sid,
                auth_token=auth_token,
            ), # Serialize frames for Twilio
        ),
    )

    # Changed from GoogleLLMService to GroqLLMService
    groq_api_key = os.getenv("GROQ_API_KEY")
    deepgram_api_key = os.getenv("DEEPGRAM_API_KEY")
    cartesia_api_key = os.getenv("CARTESIA_API_KEY")
    cartesia_voice_id = os.getenv("CARTESIA_VOICE_ID")

    if not groq_api_key:
        logger.error("GROQ_API_KEY environment variable is missing.")
        raise ValueError("GROQ_API_KEY environment variable is required.")
    if not deepgram_api_key:
        logger.error("DEEPGRAM_API_KEY environment variable is missing.")
        raise ValueError("DEEPGRAM_API_KEY environment variable is required.")
    if not cartesia_api_key:
        logger.error("CARTESIA_API_KEY environment variable is missing.")
        raise ValueError("CARTESIA_API_KEY environment variable is required.")
    if not cartesia_voice_id:
        logger.error("CARTESIA_VOICE_ID environment variable is missing.")
        raise ValueError("CARTESIA_VOICE_ID environment variable is required.")

    llm = GroqLLMService(
        api_key=groq_api_key,
        model="llama-3.3-70b-versatile",
    )

    stt = DeepgramSTTService(
        api_key=deepgram_api_key,
        audio_passthrough=True,
        settings=DeepgramSTTService.Settings(
            model="nova-3",
            language="ta",
            endpointing=400,
        ),
    )

    tts = CartesiaTTSService(
        api_key=cartesia_api_key,
        settings=CartesiaTTSService.Settings(voice=cartesia_voice_id),
        push_silence_after_stop=True,
    )

    system_prompt = (
        f"You are an AI customer support agent for a telecom company named Tasha. "
        f"You are speaking with a customer named {customer_name}. "
        f"They requested a callback regarding a '{issue_type}' issue. "
        f"Respond STRICTLY and EXCLUSIVELY in the Tamil language using the Tamil script. Do not use any English words or English letters. "
        f"Start by saying EXACTLY: 'வணக்கம் {customer_name}, நீங்கள் எப்படி இருக்கிறீர்கள்?' Do not add any other introductory text. "
        "Wait for their response, and then acknowledge their problem naturally in Tamil. "
        "Ask one question at a time. Keep your sentences short and conversational. Be helpful and empathetic. "
        "Do not include special characters, complex formatting, or markdown in your answers. Troubleshoot the issue with them step-by-step entirely in Tamil."
    )

    # Initialize messages with the system prompt
    context = LLMContext()
    context.add_message({"role": "system", "content": system_prompt})
    context_aggregator = LLMContextAggregatorPair(context)

    # AudioBufferProcessor captures all audio passing through it
    audiobuffer = AudioBufferProcessor(user_continuous_stream=not testing)

    # Define the pipeline for audio and text processing
    pipeline = Pipeline(
        [
            transport.input(),  # WebSocket input from Twilio (audio from caller)
            stt,  # Speech-To-Text: Converts caller's audio to text
            context_aggregator.user(), # Aggregates user's text into LLM context
            llm,  # LLM (Groq): Generates bot's text response
            tts,  # Text-To-Speech: Converts bot's text response to audio
            transport.output(),  # WebSocket output to Twilio (audio to caller)
            audiobuffer,  # Buffers all audio (inbound and outbound) for recording
            context_aggregator.assistant(), # Aggregates bot's text into LLM context
        ]
    )

    # Define pipeline task parameters
    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            audio_in_sample_rate=8000, # Twilio audio input sample rate
            audio_out_sample_rate=8000, # Twilio audio output sample rate
            allow_interruptions=True, # Allow bot to be interrupted by user speech
        ),
    )

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        """
        Handler for when the WebSocket client (Twilio) connects.
        Starts audio recording and kicks off the conversation.
        """
        await audiobuffer.start_recording()
        logger.debug("Sending initial context frame to LLM to kick off conversation.")
        # Sending a context frame triggers the LLM to generate its first response
        # based on the system prompt (which includes introduction and first question).
        await task.queue_frames([LLMContextFrame(context)])


    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        """
        Handler for when the WebSocket client (Twilio) disconnects.
        Saves any remaining buffered audio and the full conversation log.
        """
        logger.info("Client disconnected. Audio chunks are being saved in the 'recordings' directory.")
        
        # Save conversation log to JSON file
        # The stream_sid provides a unique identifier for each call session
        conversation_filename = os.path.join(
            CONVERSATION_LOG_DIR,
            f"conversation_{stream_sid}_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        )
        try:
            # context.messages contains the full chat history (system, user, assistant roles)
            async with aiofiles.open(conversation_filename, "w") as f:
                await f.write(json.dumps(context.messages, indent=2))
            logger.info(f"Conversation log saved to {conversation_filename}")
        except Exception as e:
            logger.error(f"Error saving conversation log: {e}")

        # Cancel the pipeline task to clean up resources
        await task.cancel()
        logger.info("Pipeline task cancelled.")

    @audiobuffer.event_handler("on_audio_data")
    async def on_audio_data(buffer, audio, sample_rate, num_channels):
        """
        Handler for when AudioBufferProcessor has accumulated audio data.
        This is called periodically to save chunks of the conversation audio.
        """
        # Safely get port for server_name, fallback to 'unknown' if not available
        port = getattr(getattr(websocket_client, 'client', None), 'port', 'unknown')
        server_name = f"server_{port}"
        # The event handler signature should provide audio, sample_rate, num_channels
        # If not, log an error
        if audio is None or sample_rate is None or num_channels is None:
            logger.error("on_audio_data handler missing required parameters: audio, sample_rate, num_channels")
            return
        await save_audio(server_name, audio, sample_rate, num_channels)

    # Initialize and run the pipeline runner
    runner = PipelineRunner(handle_sigint=False, force_gc=True)

    # Run the main pipeline task until it's cancelled
    await runner.run(task)

