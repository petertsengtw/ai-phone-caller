#
# ai-phone-caller - Open Source AI Phone Agent
# Built for OpenClaw (https://openclaw.ai)
#
# SPDX-License-Identifier: MIT
#

"""bot.py - Pipecat voice pipeline for outbound phone calls.

Handles the real-time voice conversation: listens (STT), thinks (LLM),
speaks (TTS). Supports multiple providers via environment config.

Updated to Pipecat 0.0.106 API patterns (March 2026).
"""

import asyncio
import os
import sys

from dotenv import load_dotenv
from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import LLMRunFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext
from pipecat.runner.types import RunnerArguments
from pipecat.runner.utils import parse_telephony_websocket
from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.transports.base_transport import BaseTransport
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)

from languages import LANGUAGES, cartesia_voice_id, resolve_language

load_dotenv(override=True)

logger.remove(0)
logger.add(sys.stderr, level="DEBUG")


# --------------- Provider Factories --------------- #


def create_stt(language: str):
    """Create Speech-to-Text service (Deepgram)."""
    from pipecat.services.deepgram.stt import DeepgramSTTService

    return DeepgramSTTService(
        api_key=os.getenv("DEEPGRAM_API_KEY"),
        settings=DeepgramSTTService.Settings(
            language=LANGUAGES[language]["stt_language"],
            model="nova-3",
            smart_format=True,
            interim_results=True,
            endpointing=400,       # 400ms Stille = Satz fertig (war 300)
            utterance_end_ms=1200,  # 1.2s bis Aeusserung als beendet gilt (war 1000)
        ),
    )


def create_tts(language: str):
    """Create Text-to-Speech service based on TTS_PROVIDER env."""
    provider = os.getenv("TTS_PROVIDER", "cartesia").lower()

    if provider == "cartesia":
        from pipecat.services.cartesia.tts import CartesiaTTSService, GenerationConfig

        return CartesiaTTSService(
            api_key=os.getenv("CARTESIA_API_KEY"),
            settings=CartesiaTTSService.Settings(
                voice=cartesia_voice_id(language),
                language=LANGUAGES[language]["tts_language"],
                generation_config=GenerationConfig(
                    speed=1.1,
                    emotion="calm",
                ),
            ),
        )
    elif provider == "openai":
        from pipecat.services.openai.tts import OpenAITTSService

        return OpenAITTSService(
            api_key=os.getenv("OPENAI_API_KEY"),
            voice=os.getenv("OPENAI_TTS_VOICE", "onyx"),
        )
    else:
        raise ValueError(f"Unknown TTS provider: {provider}. Use: cartesia, openai")


def create_llm():
    """Create LLM service based on LLM_PROVIDER env."""
    provider = os.getenv("LLM_PROVIDER", "openai").lower()

    if provider == "openai":
        from pipecat.services.openai.llm import OpenAILLMService

        base_url = os.getenv("OPENAI_BASE_URL", None)
        return OpenAILLMService(
            api_key=os.getenv("OPENAI_API_KEY"),
            base_url=base_url,
            settings=OpenAILLMService.Settings(
                model=os.getenv("LLM_MODEL", "gpt-4.1"),
            ),
        )
    elif provider == "google":
        from pipecat.services.google.llm import GoogleLLMService

        return GoogleLLMService(
            api_key=os.getenv("GOOGLE_API_KEY"),
        )
    elif provider == "anthropic":
        from pipecat.services.anthropic.llm import AnthropicLLMService

        return AnthropicLLMService(
            api_key=os.getenv("ANTHROPIC_API_KEY"),
            settings=AnthropicLLMService.Settings(
                model=os.getenv("LLM_MODEL", "claude-sonnet-4-20250514"),
            ),
        )
    else:
        raise ValueError(f"Unknown LLM provider: {provider}. Use: openai, google, anthropic")


# --------------- Bot Pipeline --------------- #


async def run_bot(transport: BaseTransport, handle_sigint: bool, call_config: dict = None):
    """Run the voice pipeline with configured providers."""
    config = call_config or {}
    language = resolve_language(config.get("language"))
    lang = LANGUAGES[language]
    logger.info(f"Call language: {language}")

    # Build system prompt from task description
    system_prompt = config.get("system_prompt", lang["system_prompt"])
    task_desc = config.get("task", "")
    caller_name = config.get("caller_name", "")

    full_prompt = system_prompt.replace("[CALLER_NAME]", caller_name)
    if task_desc:
        full_prompt += f"\n\n{lang['task_header']}\n{task_desc}"

    llm = create_llm()
    stt = create_stt(language)
    tts = create_tts(language)

    # Use OpenAILLMContext (works with all LLM providers in Pipecat)
    messages = [
        {"role": "system", "content": full_prompt},
    ]
    context = OpenAILLMContext(messages)
    context_aggregator = llm.create_context_aggregator(context)

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            context_aggregator.user(),
            llm,
            tts,
            transport.output(),
            context_aggregator.assistant(),
        ]
    )

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            audio_in_sample_rate=8000,
            audio_out_sample_rate=8000,
            enable_metrics=True,
            enable_usage_metrics=True,
        ),
    )

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info("Call connected - speaking immediately")
        # Speak immediately - don't wait, the caller expects someone to talk
        messages.append({
            "role": "system",
            "content": lang["on_connect"],
        })
        await task.queue_frame(LLMRunFrame())

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info("Call ended")
        await task.cancel()

    runner = PipelineRunner(handle_sigint=handle_sigint)
    await runner.run(task)


async def bot(runner_args: RunnerArguments, call_config: dict = None):
    """Main bot entry point. Called by server.py when WebSocket connects."""
    transport_type, call_data = await parse_telephony_websocket(runner_args.websocket)
    logger.info(f"Transport: {transport_type}")

    body_data = call_data.get("body", {})
    to_number = body_data.get("to_number")
    from_number = body_data.get("from_number")
    logger.info(f"Call: {from_number} -> {to_number}")

    # Extract call config from stream parameters if passed
    if not call_config:
        call_config = {
            "task": body_data.get("task", ""),
            "caller_name": body_data.get("caller_name", ""),
            "language": body_data.get("language", ""),
        }

    serializer = TwilioFrameSerializer(
        stream_sid=call_data["stream_id"],
        call_sid=call_data["call_id"],
        account_sid=os.getenv("TWILIO_ACCOUNT_SID", ""),
        auth_token=os.getenv("TWILIO_AUTH_TOKEN", ""),
    )

    transport = FastAPIWebsocketTransport(
        websocket=runner_args.websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            add_wav_header=False,
            vad_analyzer=SileroVADAnalyzer(
                params=VADParams(
                    confidence=0.8,    # Hoehere Confidence noetig (war 0.7)
                    start_secs=0.4,    # 0.4s bevor "User spricht" erkannt (war 0.3)
                    stop_secs=1.0,     # 1.0s Stille bevor Bot antwortet (war 0.8)
                    min_volume=0.6,    # Hoehere Mindestlautstaerke (filtert Hintergrund)
                ),
            ),
            serializer=serializer,
        ),
    )

    await run_bot(transport, runner_args.handle_sigint, call_config)
