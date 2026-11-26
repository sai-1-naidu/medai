from datetime import datetime
import json
import logging

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import ValidationError

from app.agent.tools import (
    book_appointment,
    cancel_appointment,
    check_doctor_availability,
    prepare_appointment_cancellation,
    prepare_appointment_booking,
)
from app.agent.confirmation import (
    classify_confirmation,
    complete_pending_cancellation,
    complete_pending_booking,
    create_pending_cancellation,
    create_pending_booking,
    discard_pending_cancellation,
    discard_pending_booking,
    get_pending_cancellation,
    get_pending_booking,
)
from app.config import GROQ_API_KEY, GROQ_MODEL
from app.provider_errors import log_provider_http_error, safe_provider_detail
from app.schemas import BookingRequest, CancellationToolRequest, ChatRequest

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/agent", tags=["voice assistant"])

SYSTEM_PROMPT = (
    "You are MediVoice, a warm and concise clinic receptionist. "
    "Help patients find doctors and manage appointments using the provided tools. "
    "Never diagnose, recommend treatment, or prescribe medication. "
    "For an emergency, tell the patient to contact local emergency services immediately. "
    "Never say an appointment is booked or cancelled unless the relevant tool succeeds. "
    "When the patient has provided a doctor, exact slot, and phone number, call the "
    "book_appointment tool to prepare a review summary. The server will not book until "
    "the patient explicitly confirms that summary in a later message. Do not ask for "
    "an exact confirmation phrase; accept ordinary clear affirmatives. "
    "For cancellation, call cancel_appointment to validate and prepare a review summary. "
    "The server will not cancel until the patient explicitly confirms that summary in a "
    "later message. Do not ask for an exact confirmation phrase. "
    "Never ask for or repeat sensitive medical details that are not needed for scheduling."
)

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "check_doctor_availability",
            "description": "Find available appointments for a medical speciality on a date.",
            "parameters": {
                "type": "object",
                "properties": {
                    "speciality": {"type": "string"},
                    "date": {"type": "string", "description": "Date in YYYY-MM-DD format"},
                },
                "required": ["speciality", "date"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "book_appointment",
            "description": (
                "Submit a complete proposed booking for validation and patient read-back. "
                "The server will wait for explicit confirmation before creating the booking."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "doctor_id": {"type": "integer"},
                    "slot_id": {"type": "integer"},
                    "patient_phone": {"type": "string"},
                },
                "required": ["doctor_id", "slot_id", "patient_phone"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_appointment",
            "description": "Cancel an appointment after confirming its ID and patient phone.",
            "parameters": {
                "type": "object",
                "properties": {
                    "appointment_id": {"type": "integer"},
                    "patient_phone": {"type": "string"},
                },
                "required": ["appointment_id", "patient_phone"],
                "additionalProperties": False,
            },
        },
    },
]

TOOL_NAMES = {tool["function"]["name"] for tool in TOOLS}


def _run_tool(name: str, arguments: dict) -> dict:
    if name == "check_doctor_availability":
        return check_doctor_availability(
            speciality=arguments["speciality"],
            date=arguments["date"],
        )
    if name == "book_appointment":
        return book_appointment(
            doctor_id=arguments["doctor_id"],
            slot_id=arguments["slot_id"],
            patient_phone=arguments["patient_phone"],
        )
    if name == "cancel_appointment":
        return cancel_appointment(
            appointment_id=arguments["appointment_id"],
            patient_phone=arguments["patient_phone"],
        )
    return {"success": False, "error": "Unsupported assistant action."}


def _raise_provider_error(exc: httpx.HTTPStatusError) -> None:
    response = exc.response
    status_code = response.status_code
    log_provider_http_error(logger, "Groq chat", response)

    if status_code in (401, 403):
        raise HTTPException(
            status_code=502,
            detail="The AI provider rejected this service's credentials.",
        ) from exc
    if status_code in (400, 404):
        raise HTTPException(
            status_code=502,
            detail="The AI provider rejected the configured model or request.",
        ) from exc
    if status_code == 429:
        retry_after = response.headers.get("Retry-After")
        headers = {"Retry-After": retry_after} if retry_after and retry_after.isdigit() else None
        raise HTTPException(
            status_code=503,
            detail="The AI provider is rate-limiting requests. Please try again shortly.",
            headers=headers,
        ) from exc
    raise HTTPException(
        status_code=502,
        detail="The AI provider could not complete the request.",
    ) from exc


def _invalid_tool_response(reason: str) -> HTTPException:
    logger.warning("Groq returned an invalid tool call: %s", reason)
    return HTTPException(status_code=502, detail="The AI returned invalid action details.")


def _format_booking_summary(preview: dict) -> str:
    start_time = datetime.fromisoformat(preview["start_time"])
    date = f"{start_time:%A}, {start_time:%B} {start_time.day}, {start_time:%Y}"
    time = start_time.strftime("%I:%M %p").lstrip("0")
    return (
        f"Please review: {preview['doctor_name']} ({preview['speciality']}), "
        f"{date} at {time}; phone number {preview['patient_phone']}. "
        "Should I book this? You can say yes, confirm, go ahead, or ask to change something."
    )


def _prepare_booking_response(arguments: dict) -> dict:
    try:
        booking = BookingRequest.model_validate(arguments)
    except ValidationError:
        logger.warning("Groq supplied invalid booking proposal details.")
        return {
            "reply": "I couldn't verify those booking details. Please check the doctor, time, and phone number.",
            "actions": [],
            "pending_booking_id": None,
        }

    preview = prepare_appointment_booking(
        doctor_id=booking.doctor_id,
        slot_id=booking.slot_id,
        patient_phone=booking.patient_phone,
    )
    if not preview["success"]:
        logger.info(
            "Booking proposal could not be prepared (slot_id=%s, code=%s).",
            booking.slot_id,
            preview.get("code", "unknown"),
        )
        if preview.get("code") == "unavailable":
            reply = "That time is no longer available. I haven't booked anything. Please choose another slot."
        else:
            reply = "I couldn't verify that doctor and time. Please check the details or choose another slot."
        return {"reply": reply, "actions": [], "pending_booking_id": None}

    pending_booking_id = create_pending_booking({
        "doctor_id": preview["doctor_id"],
        "slot_id": preview["slot_id"],
        "patient_phone": preview["patient_phone"],
    })
    logger.info(
        "Prepared booking confirmation (doctor_id=%s, slot_id=%s).",
        preview["doctor_id"],
        preview["slot_id"],
    )
    return {
        "reply": _format_booking_summary(preview),
        "actions": [],
        "pending_booking_id": pending_booking_id,
    }


def _format_cancellation_summary(preview: dict) -> str:
    start_time = datetime.fromisoformat(preview["start_time"])
    date = f"{start_time:%A}, {start_time:%B} {start_time.day}, {start_time:%Y}"
    time = start_time.strftime("%I:%M %p").lstrip("0")
    return (
        f"Please review the cancellation: appointment {preview['appointment_id']} "
        f"with {preview['doctor_name']} ({preview['speciality']}), {date} at {time}; "
        f"phone number {preview['patient_phone']}. Should I cancel this appointment?"
    )


def _prepare_cancellation_response(arguments: dict) -> dict:
    try:
        cancellation = CancellationToolRequest.model_validate(arguments)
    except ValidationError:
        logger.warning("Groq supplied invalid cancellation proposal details.")
        return {
            "reply": "I couldn't verify those appointment details. Please check the appointment ID and phone number.",
            "actions": [],
            "pending_cancellation_id": None,
        }

    preview = prepare_appointment_cancellation(
        appointment_id=cancellation.appointment_id,
        patient_phone=cancellation.patient_phone,
    )
    if not preview["success"]:
        logger.info(
            "Cancellation proposal could not be prepared (appointment_id=%s, code=%s).",
            cancellation.appointment_id,
            preview.get("code", "unknown"),
        )
        return {
            "reply": (
                "I couldn't verify that appointment and phone number. "
                "Please check the details before trying again."
            ),
            "actions": [],
            "pending_cancellation_id": None,
        }

    pending_cancellation_id = create_pending_cancellation({
        "appointment_id": preview["appointment_id"],
        "patient_phone": preview["patient_phone"],
        "doctor_name": preview["doctor_name"],
        "speciality": preview["speciality"],
        "slot_id": preview["slot_id"],
        "start_time": preview["start_time"],
        "end_time": preview["end_time"],
    })
    logger.info(
        "Prepared cancellation confirmation (appointment_id=%s).",
        preview["appointment_id"],
    )
    return {
        "reply": _format_cancellation_summary(preview),
        "actions": [],
        "pending_cancellation_id": pending_cancellation_id,
    }


def _booking_success_response(result: dict) -> dict:
    start_time = datetime.fromisoformat(result["start_time"])
    date = f"{start_time:%A}, {start_time:%B} {start_time.day}, {start_time:%Y}"
    time = start_time.strftime("%I:%M %p").lstrip("0")
    return {
        "reply": (
            f"You're booked with {result['doctor_name']} ({result['speciality']}) "
            f"for {date} at {time}. Appointment ID {result['appointment_id']}."
        ),
        "actions": [{"name": "book_appointment", "result": result}],
        "pending_booking_id": None,
    }


def _handle_pending_booking(request: ChatRequest) -> dict:
    token = request.pending_booking_id
    pending = get_pending_booking(token) if token else None
    if pending is None:
        return {
            "reply": "That booking confirmation has expired. Please check availability and start again.",
            "actions": [],
            "pending_booking_id": None,
        }

    decision = classify_confirmation(request.messages[-1].content)
    if decision == "reject":
        discard_pending_booking(token)
        logger.info("Patient declined pending booking (slot_id=%s).", pending["details"]["slot_id"])
        return {
            "reply": "No problem. I haven't booked anything. What would you like to change?",
            "actions": [],
            "pending_booking_id": None,
        }
    if decision != "confirm":
        return {
            "reply": "I haven't booked it yet. You can confirm, or tell me what you'd like to change.",
            "actions": [],
            "pending_booking_id": token,
        }

    if pending["result"] is not None:
        return _booking_success_response(pending["result"])

    details = pending["details"]
    try:
        result = book_appointment(
            doctor_id=details["doctor_id"],
            slot_id=details["slot_id"],
            patient_phone=details["patient_phone"],
            idempotent=True,
        )
    except (KeyError, TypeError, ValueError) as exc:
        logger.error(
            "Confirmed booking tool raised %s (slot_id=%s).",
            type(exc).__name__,
            details["slot_id"],
        )
        result = {"success": False, "error": "The booking could not be completed."}

    if not result.get("success"):
        logger.warning(
            "Confirmed booking failed (slot_id=%s, code=%s).",
            details["slot_id"],
            result.get("code", "unknown"),
        )
        code = result.get("code")
        if code == "unavailable":
            reply = "That time is no longer available. I haven't booked anything. Please choose another slot."
        elif code == "not_found":
            reply = "I couldn't find that doctor or time slot. Please check the details and try again."
        else:
            reply = "I couldn't complete the booking. Your details are saved; you can retry or choose another time."
        return {
            "reply": reply,
            "actions": [{"name": "book_appointment", "result": result}],
            "pending_booking_id": token,
        }

    complete_pending_booking(token, result)
    logger.info("Confirmed booking completed (slot_id=%s).", details["slot_id"])
    return _booking_success_response(result)


def _cancellation_success_response(result: dict, details: dict) -> dict:
    return {
        "reply": (
            f"Your appointment with {details['doctor_name']} "
            f"({details['speciality']}) has been cancelled."
        ),
        "actions": [{"name": "cancel_appointment", "result": result}],
        "pending_cancellation_id": None,
    }


def _handle_pending_cancellation(request: ChatRequest) -> dict:
    token = request.pending_cancellation_id
    pending = get_pending_cancellation(token) if token else None
    if pending is None:
        return {
            "reply": "That cancellation confirmation has expired. Please review the appointment and start again.",
            "actions": [],
            "pending_cancellation_id": None,
        }

    decision = classify_confirmation(
        request.messages[-1].content,
        action="cancellation",
    )
    if decision == "reject":
        discard_pending_cancellation(token)
        logger.info(
            "Patient declined pending cancellation (appointment_id=%s).",
            pending["details"]["appointment_id"],
        )
        return {
            "reply": "No problem. The appointment is still active.",
            "actions": [],
            "pending_cancellation_id": None,
        }
    if decision != "confirm":
        return {
            "reply": "I haven't cancelled it. You can confirm or ask me to keep the appointment.",
            "actions": [],
            "pending_cancellation_id": token,
        }

    if pending["result"] is not None:
        return _cancellation_success_response(pending["result"], pending["details"])

    details = pending["details"]
    try:
        result = cancel_appointment(
            appointment_id=details["appointment_id"],
            patient_phone=details["patient_phone"],
            idempotent=True,
        )
    except (KeyError, TypeError, ValueError) as exc:
        logger.error(
            "Confirmed cancellation tool raised %s (appointment_id=%s).",
            type(exc).__name__,
            details["appointment_id"],
        )
        result = {"success": False, "error": "The cancellation could not be completed."}

    if not result.get("success"):
        logger.warning(
            "Confirmed cancellation failed (appointment_id=%s, code=%s).",
            details["appointment_id"],
            result.get("code", "unknown"),
        )
        return {
            "reply": "I couldn't cancel the appointment. It remains unchanged; you can retry or keep it.",
            "actions": [{"name": "cancel_appointment", "result": result}],
            "pending_cancellation_id": token,
        }

    complete_pending_cancellation(token, result)
    logger.info(
        "Confirmed cancellation completed (appointment_id=%s).",
        details["appointment_id"],
    )
    return _cancellation_success_response(result, details)


@router.post("/chat")
async def chat(request: ChatRequest):
    if request.pending_booking_id is not None:
        return _handle_pending_booking(request)
    if request.pending_cancellation_id is not None:
        return _handle_pending_cancellation(request)

    if not GROQ_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="The AI assistant is not configured. Add GROQ_API_KEY to backend/.env.",
        )

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(message.model_dump() for message in request.messages)
    actions = []

    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            for _ in range(6):
                response = await client.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                    json={
                        "model": GROQ_MODEL,
                        "messages": messages,
                        "tools": TOOLS,
                        "tool_choice": "auto",
                        "temperature": 0.3,
                    },
                )
                response.raise_for_status()
                try:
                    data = response.json()
                except json.JSONDecodeError as exc:
                    logger.warning("Groq returned invalid JSON.")
                    raise HTTPException(
                        status_code=502,
                        detail="The AI provider returned an invalid response.",
                    ) from exc
                if (
                    not isinstance(data, dict)
                    or not isinstance(data.get("choices"), list)
                    or not data["choices"]
                    or not isinstance(data["choices"][0], dict)
                ):
                    logger.warning("Groq returned an empty or malformed completion.")
                    raise HTTPException(status_code=502, detail="The AI returned an empty response.")

                assistant_message = data["choices"][0].get("message", {})
                if not isinstance(assistant_message, dict):
                    raise HTTPException(status_code=502, detail="The AI returned an invalid response.")
                tool_calls = assistant_message.get("tool_calls", [])
                if not isinstance(tool_calls, list):
                    raise HTTPException(status_code=502, detail="The AI returned invalid action details.")
                if not tool_calls:
                    reply = assistant_message.get("content")
                    if any(not action["result"].get("success") for action in actions):
                        return {
                            "reply": "I couldn't complete the appointment action. Please review the details and try again.",
                            "actions": actions,
                        }
                    if not isinstance(reply, str) or not reply.strip():
                        raise HTTPException(status_code=502, detail="The AI returned an empty response.")
                    return {"reply": reply.strip(), "actions": actions}

                messages.append(assistant_message)
                parsed_tool_calls = []
                for tool_call in tool_calls:
                    if not isinstance(tool_call, dict):
                        raise _invalid_tool_response("call must be an object")
                    function = tool_call.get("function", {})
                    if not isinstance(function, dict):
                        raise _invalid_tool_response("function must be an object")
                    name = function.get("name", "")
                    tool_call_id = tool_call.get("id")
                    raw_arguments = function.get("arguments")
                    if not isinstance(tool_call_id, str) or not tool_call_id:
                        raise _invalid_tool_response("call id is missing")
                    if not isinstance(name, str) or name not in TOOL_NAMES:
                        raise _invalid_tool_response("function name is unsupported")
                    if not isinstance(raw_arguments, str):
                        raise _invalid_tool_response("arguments must be a JSON string")
                    try:
                        arguments = json.loads(raw_arguments)
                    except json.JSONDecodeError as exc:
                        raise _invalid_tool_response("arguments are not valid JSON") from exc
                    if not isinstance(arguments, dict):
                        raise _invalid_tool_response("arguments must be an object")
                    parsed_tool_calls.append((tool_call_id, name, arguments))

                booking_call = next(
                    (
                        arguments
                        for _, name, arguments in parsed_tool_calls
                        if name == "book_appointment"
                    ),
                    None,
                )
                cancellation_call = next(
                    (
                        arguments
                        for _, name, arguments in parsed_tool_calls
                        if name == "cancel_appointment"
                    ),
                    None,
                )
                if booking_call is not None and cancellation_call is not None:
                    logger.warning("Groq returned multiple appointment mutations in one response.")
                    return {
                        "reply": "I couldn't safely process both appointment actions at once. Please request one action at a time.",
                        "actions": [],
                    }
                if booking_call is not None:
                    return _prepare_booking_response(booking_call)
                if cancellation_call is not None:
                    return _prepare_cancellation_response(cancellation_call)

                for tool_call_id, name, arguments in parsed_tool_calls:
                    try:
                        result = _run_tool(name, arguments)
                    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                        result = {"success": False, "error": "The assistant supplied invalid action details."}
                    if name in {"book_appointment", "cancel_appointment"}:
                        actions.append({"name": name, "result": result})
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "content": json.dumps(result),
                    })
    except httpx.TimeoutException as exc:
        logger.warning("Groq request timed out (%s): %s", type(exc).__name__, safe_provider_detail(str(exc)))
        raise HTTPException(status_code=504, detail="The AI assistant timed out. Please try again.") from exc
    except httpx.HTTPStatusError as exc:
        _raise_provider_error(exc)
    except httpx.RequestError as exc:
        logger.warning("Groq connection failed (%s): %s", type(exc).__name__, safe_provider_detail(str(exc)))
        raise HTTPException(status_code=502, detail="The AI assistant is temporarily unavailable.") from exc

    raise HTTPException(status_code=502, detail="The assistant could not complete that action.")
