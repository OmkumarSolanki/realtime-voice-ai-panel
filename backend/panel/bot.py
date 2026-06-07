"""Builds and runs the Pipecat pipeline for one WebRTC connection."""

from loguru import logger

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.processors.frameworks.rtvi import RTVIObserver, RTVIProcessor
from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.services.deepgram.stt import DeepgramSTTService, LiveOptions
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport

from .config import config
from .orchestrator import PanelProcessor
from .voices import build_voice_pool


async def run_bot(connection: SmallWebRTCConnection):
    """Build the pipeline for this connection and run it until the client leaves."""
    voice_pool = await build_voice_pool()

    transport = SmallWebRTCTransport(
        webrtc_connection=connection,
        params=TransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=config.SAMPLE_RATE,
            audio_out_sample_rate=config.SAMPLE_RATE,
        ),
    )

    # stop_secs=1.2 -> the user can pause mid-sentence without "finishing" their turn.
    vad = VADProcessor(vad_analyzer=SileroVADAnalyzer(params=VADParams(stop_secs=1.2)))

    stt = DeepgramSTTService(
        api_key=config.DEEPGRAM_API_KEY,
        live_options=LiveOptions(
            model="nova-2",
            language="en-US",
            smart_format=True,
            interim_results=True,
            # Longer endpointing + utterance-end so a brief pause isn't treated as the end of
            # the user's turn. The orchestrator also debounces, so chunked finals are merged.
            endpointing=800,
            utterance_end_ms=1000,
        ),
    )

    tts = CartesiaTTSService(
        api_key=config.CARTESIA_API_KEY,
        voice_id=voice_pool[0]["id"],
        model=config.CARTESIA_MODEL,
        sample_rate=config.SAMPLE_RATE,
    )

    rtvi = RTVIProcessor(transport=transport)
    panel = PanelProcessor(voice_pool=voice_pool)

    pipeline = Pipeline(
        [
            transport.input(),
            rtvi,
            vad,
            stt,
            panel,
            tts,
            transport.output(),
        ]
    )

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            audio_in_sample_rate=config.SAMPLE_RATE,
            audio_out_sample_rate=config.SAMPLE_RATE,
            enable_metrics=True,
        ),
        observers=[RTVIObserver(rtvi)],
    )

    @rtvi.event_handler("on_client_ready")
    async def _on_client_ready(rtvi):
        await rtvi.set_bot_ready()
        await panel.greet()

    @rtvi.event_handler("on_client_message")
    async def _on_client_message(rtvi, message):
        await panel.handle_client_message(
            getattr(message, "type", None), getattr(message, "data", None)
        )

    @transport.event_handler("on_client_disconnected")
    async def _on_disconnected(_transport, _client):
        logger.info("Client disconnected; cancelling task.")
        await task.cancel()

    runner = PipelineRunner(handle_sigint=False)
    logger.info("Panel bot starting.")
    await runner.run(task)
    logger.info("Panel bot finished.")
