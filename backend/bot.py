import asyncio
import re
import sys
from datetime import datetime

from loguru import logger

from app.agent.confirmation import (
    assistant_requested_confirmation,
    classify_confirmation,
)
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.frames.frames import (
    Frame,
    FunctionCallResultProperties,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMRunFrame,
    LLMTextFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.runner.types import RunnerArguments, SmallWebRTCRunnerArguments
from pipecat.services.llm_service import FunctionCallParams, LLMService
from pipecat.services.elevenlabs.stt import ElevenLabsRealtimeSTTService
from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
from pipecat.services.groq.llm import GroqLLMService
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.transport import (
    SmallWebRTCClient,
    SmallWebRTCCallbacks,
    SmallWebRTCInputTransport,
    SmallWebRTCOutputTransport,
)
from app.config import (
    ELEVENLABS_API_KEY,
    ELEVENLABS_VOICE_ID,
    GROQ_API_KEY,
    GROQ_MODEL,
    validate_api_keys,
)
from app.agent.tools import (
    book_appointment,
    cancel_appointment,
    check_doctor_availability,
    normalize_phone,
    prepare_appointment_booking,
)
from app.agent.voice_confirmation import VoiceBookingSession, VoiceBookingStatus
from app.schemas import BookingRequest


SYSTEM_PROMPT = """
You are MediVoice AI, a friendly clinic receptionist.

Your responsibilities:
- Welcome patients politely.
- Help patients find doctors and appointment availability.
- For booking, collect the doctor, exact slot, and phone number, then call
  prepare_appointment_booking. Read back the returned doctor, speciality, date,
  time, and phone number and ask whether to book.
- The server holds a separate pending booking state and handles confirmation
  before your response is spoken. Accept natural answers; never require an exact phrase.
- If the patient asks a question while a booking is pending, answer from known
  information, do not claim the booking is complete, and preserve the pending booking.
- Do not invent a clinic address or other unavailable details.
- For cancellation, read back the appointment ID and phone number and wait for a
  clear, explicit confirmation before calling the cancellation tool.
- Only report a booking or cancellation as successful when its tool returns success.
- Keep responses short and natural because they will be spoken aloud.
- Never diagnose medical conditions or prescribe medication.
- If a patient describes an emergency, advise them to seek emergency medical help immediately.
"""

PIPECAT_TOOLS = ToolsSchema([
    FunctionSchema(
        name="check_doctor_availability",
        description="Find available doctors and appointment slots for a speciality and date.",
        properties={
            "speciality": {"type": "string", "description": "Doctor speciality"},
            "date": {"type": "string", "description": "Date in YYYY-MM-DD format"},
        },
        required=["speciality", "date"],
    ),
    FunctionSchema(
        name="prepare_appointment_booking",
        description=(
            "Validate the proposed doctor, slot, and phone number and prepare a read-back "
            "summary. This does not create an appointment; the patient must confirm later."
        ),
        properties={
            "doctor_id": {"type": "integer"},
            "slot_id": {"type": "integer"},
            "patient_phone": {"type": "string"},
        },
        required=["doctor_id", "slot_id", "patient_phone"],
    ),
    FunctionSchema(
        name="cancel_appointment",
        description="Cancel an appointment only after the patient clearly confirms the read-back.",
        properties={
            "appointment_id": {"type": "integer"},
            "patient_phone": {"type": "string"},
        },
        required=["appointment_id", "patient_phone"],
    ),
])


def _ensure_supported_python() -> None:
    if sys.version_info < (3, 11):
        raise SystemExit("The Pipecat WebRTC runner requires Python 3.11 or newer.")


class VoiceBookingResponseProcessor(FrameProcessor):
    def __init__(self, context: LLMContext, booking_state: VoiceBookingSession) -> None:
        super().__init__(name="voice-booking-confirmation")
        self._context = context
        self._booking_state = booking_state
        self._response_text: list[str] = []
        self._suppress_model_response = False

    def _latest_user_message(self) -> str:
        for message in reversed(self._context.get_messages()):
            if message.get("role") == "user" and isinstance(message.get("content"), str):
                return message["content"]
        return ""

    @staticmethod
    def _format_result(result: dict) -> str:
        start_time = datetime.fromisoformat(result["start_time"])
        date = f"{start_time:%A}, {start_time:%B} {start_time.day}, {start_time:%Y}"
        time = start_time.strftime("%I:%M %p").lstrip("0")
        return (
            f"Your appointment is confirmed with {result['doctor_name']} "
            f"({result['speciality']}) on {date} at {time}. "
            f"Your booking ID is {result['appointment_id']}."
        )

    def _outcome_text(self, outcome: dict) -> str:
        if outcome.get("reply"):
            return outcome["reply"]
        status = outcome["status"]
        if status == VoiceBookingStatus.REJECTED:
            return "Okay, I haven't booked anything. What would you like to change?"
        if status == VoiceBookingStatus.FAILED:
            return "I couldn't complete the booking. Your details are still saved. Would you like me to try again?"
        result = outcome.get("result")
        if result and result.get("success"):
            return self._format_result(result)
        return "I couldn't confirm the booking. I haven't created an appointment."

    async def _push_replacement_response(self, text: str, direction: FrameDirection) -> None:
        await self.push_frame(LLMFullResponseStartFrame(), direction)
        await self.push_frame(LLMTextFrame(text=text), direction)
        await self.push_frame(LLMFullResponseEndFrame(), direction)

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if direction == FrameDirection.UPSTREAM:
            await self.push_frame(frame, direction)
            return

        if self._suppress_model_response:
            if isinstance(frame, LLMFullResponseEndFrame):
                self._suppress_model_response = False
                self._response_text.clear()
            return

        if isinstance(frame, LLMFullResponseStartFrame):
            self._response_text.clear()
            outcome = await self._booking_state.handle_confirmation(
                self._latest_user_message()
            )
            if outcome is not None and outcome.get("handled"):
                self._suppress_model_response = True
                await self._push_replacement_response(
                    self._outcome_text(outcome),
                    direction,
                )
                return
        elif isinstance(frame, LLMTextFrame):
            self._response_text.append(frame.text)
        elif isinstance(frame, LLMFullResponseEndFrame):
            await self._booking_state.mark_readback_complete(
                "".join(self._response_text)
            )
            self._response_text.clear()

        await self.push_frame(frame, direction)


def _has_explicit_confirmation(
    params: FunctionCallParams,
    action: str,
    readback_values: tuple[str, ...],
) -> bool:
    messages = params.context.get_messages()
    user_index = next(
        (
            index
            for index in range(len(messages) - 1, -1, -1)
            if messages[index].get("role") == "user"
        ),
        None,
    )
    if user_index is None:
        return False

    user_message = messages[user_index].get("content")
    if not isinstance(user_message, str) or classify_confirmation(user_message, action) != "confirm":
        return False

    assistant_message = next(
        (
            message.get("content")
            for message in reversed(messages[:user_index])
            if message.get("role") == "assistant"
            and isinstance(message.get("content"), str)
        ),
        None,
    )
    if not isinstance(assistant_message, str) or not assistant_requested_confirmation(
        assistant_message,
        action,
    ):
        return False

    spoken_digits = re.sub(r"\D", "", assistant_message)
    return all(value and value in spoken_digits for value in readback_values)


async def _run_voice_tool(
    params: FunctionCallParams,
    tool,
    *,
    confirmation_action: str | None = None,
    readback_values: tuple[str, ...] = (),
    idempotent: bool = False,
) -> None:
    if confirmation_action and not _has_explicit_confirmation(
        params,
        confirmation_action,
        readback_values,
    ):
        result = {"success": False, "error": "Wait for the patient to explicitly confirm the read-back."}
    else:
        try:
            arguments = dict(params.arguments)
            if idempotent:
                arguments["idempotent"] = True
            result = await asyncio.to_thread(tool, **arguments)
        except (KeyError, TypeError, ValueError):
            result = {"success": False, "error": "The assistant supplied invalid action details."}
        except Exception as exc:
            logger.error(
                "Pipecat appointment tool {} failed ({})",
                params.function_name,
                type(exc).__name__,
            )
            result = {"success": False, "error": "The appointment action could not be completed."}

    if confirmation_action and not result.get("success"):
        await params.result_callback(
            result,
            properties=FunctionCallResultProperties(run_llm=False),
        )
        failure_message = (
            "I won't make that change without your confirmation after the read-back."
            if str(result.get("error", "")).startswith("Wait for the patient to explicitly confirm")
            else "I couldn't complete that appointment action. Please check the details and try again."
        )
        await params.llm.push_frame(LLMFullResponseStartFrame())
        await params.llm.push_frame(LLMTextFrame(text=failure_message))
        await params.llm.push_frame(LLMFullResponseEndFrame())
        return

    await params.result_callback(result)


def register_appointment_tools(
    llm: LLMService,
    context: LLMContext,
    booking_state: VoiceBookingSession,
) -> None:
    async def availability(params: FunctionCallParams) -> None:
        await _run_voice_tool(params, check_doctor_availability)

    async def prepare_booking(params: FunctionCallParams) -> None:
        try:
            booking = BookingRequest.model_validate(params.arguments)
            preview = await asyncio.to_thread(
                prepare_appointment_booking,
                doctor_id=booking.doctor_id,
                slot_id=booking.slot_id,
                patient_phone=booking.patient_phone,
            )
            if preview.get("success"):
                result = {
                    "success": True,
                    "booking": await booking_state.stage(preview),
                }
            else:
                result = preview
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning(
                "Voice booking proposal was invalid (%s).",
                type(exc).__name__,
            )
            result = {
                "success": False,
                "error": "I couldn't verify those appointment details.",
            }

        await params.result_callback(result)

    async def cancellation(params: FunctionCallParams) -> None:
        phone = params.arguments.get("patient_phone")
        appointment_id = params.arguments.get("appointment_id")
        normalized_phone = normalize_phone(phone) if isinstance(phone, str) else None
        confirmation_values = (
            (normalized_phone, str(appointment_id))
            if normalized_phone and isinstance(appointment_id, int)
            else ()
        )
        await _run_voice_tool(
            params,
            cancel_appointment,
            confirmation_action="cancellation",
            readback_values=confirmation_values,
            idempotent=True,
        )

    context.set_tools(PIPECAT_TOOLS)
    llm.register_function("check_doctor_availability", availability)
    llm.register_function("prepare_appointment_booking", prepare_booking)
    llm.register_function("cancel_appointment", cancellation)


async def run_bot(runner_args: SmallWebRTCRunnerArguments):
    logger.info("Starting MediVoice AI pipeline")
    validate_api_keys()

    stt = ElevenLabsRealtimeSTTService(
        api_key=ELEVENLABS_API_KEY,
    )

    llm = GroqLLMService(
        api_key=GROQ_API_KEY,
        run_in_parallel=False,
        settings=GroqLLMService.Settings(
            model=GROQ_MODEL,
            system_instruction=SYSTEM_PROMPT,
        ),
    )

    tts = ElevenLabsTTSService(
        api_key=ELEVENLABS_API_KEY,
        settings=ElevenLabsTTSService.Settings(
            voice=ELEVENLABS_VOICE_ID,
            model="eleven_flash_v2_5",
        ),
    )

    context = LLMContext()
    booking_state = VoiceBookingSession(book_appointment)
    register_appointment_tools(llm, context, booking_state)
    booking_response_processor = VoiceBookingResponseProcessor(context, booking_state)
    user_aggregator, assistant_aggregator = (
        LLMContextAggregatorPair(
            context,
            user_params=LLMUserAggregatorParams(
                vad_analyzer=SileroVADAnalyzer(),
            ),
        )
    )

    runner = PipelineRunner(handle_sigint=runner_args.handle_sigint)
    task: PipelineTask

    async def on_client_connected(connection):
        logger.info("Patient connected")
        context.add_message({
            "role": "system",
            "content": (
                "Start the conversation by welcoming the patient "
                "and asking how you can help."
            ),
        })
        await task.queue_frames([LLMRunFrame()])

    async def on_client_disconnected(connection):
        logger.info("Patient disconnected")
        await runner.cancel()

    async def on_app_message(message, sender):
        return None

    transport_params = TransportParams(
        audio_in_enabled=True,
        audio_out_enabled=True,
    )
    client = SmallWebRTCClient(
        runner_args.webrtc_connection,
        SmallWebRTCCallbacks(
            on_client_connected=on_client_connected,
            on_client_disconnected=on_client_disconnected,
            on_app_message=on_app_message,
        ),
    )
    input_transport = SmallWebRTCInputTransport(client, transport_params)
    output_transport = SmallWebRTCOutputTransport(client, transport_params)

    pipeline = Pipeline(
        [
            input_transport,
            stt,
            user_aggregator,
            llm,
            booking_response_processor,
            tts,
            output_transport,
            assistant_aggregator,
        ]
    )

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            enable_metrics=True,
            enable_usage_metrics=True,
        ),
    )

    await runner.run(task)


async def bot(runner_args: RunnerArguments):
    if not isinstance(runner_args, SmallWebRTCRunnerArguments):
        raise ValueError("MediVoice's Pipecat bot requires a Small WebRTC connection.")
    await run_bot(runner_args)


if __name__ == "__main__":
    _ensure_supported_python()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    validate_api_keys()

    from pipecat.runner.run import main

    main()