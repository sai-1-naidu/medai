const conversation = document.querySelector("#conversation");
const chatForm = document.querySelector("#chat-form");
const chatInput = document.querySelector("#chat-input");
const micButton = document.querySelector("#mic-button");
const voiceStatus = document.querySelector("#voice-status");
const availabilityForm = document.querySelector("#availability-form");
const results = document.querySelector("#availability-results");
const phoneInput = document.querySelector("#patient-phone");
const bookingContent = document.querySelector("#booking-content");
const toast = document.querySelector("#toast");

const history = [];
let currentBooking = null;
let pendingBookingId = null;
let pendingCancellationId = null;
let recognition = null;
let mediaRecorder = null;
let audioStream = null;
let audioChunks = [];
let toastTimer;
let appointmentMutationInProgress = false;

const today = new Date();
const tomorrow = new Date(today.getFullYear(), today.getMonth(), today.getDate() + 1);
const localDate = (date) => {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
};
document.querySelector("#appointment-date").min = localDate(today);
document.querySelector("#appointment-date").value = localDate(tomorrow);

function showToast(message) {
  toast.textContent = message;
  toast.classList.add("visible");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove("visible"), 3500);
}

function setVoiceStatus(message, recording = false) {
  voiceStatus.textContent = message;
  voiceStatus.classList.toggle("recording", recording);
  micButton.classList.toggle("is-recording", recording);
  micButton.setAttribute("aria-label", recording ? "Stop voice input" : "Start voice input");
  micButton.title = recording ? "Stop voice input" : "Start voice input";
}

function appendMessage(role, text, isError = false) {
  const wrapper = document.createElement("div");
  wrapper.className = `message ${role === "user" ? "user-message" : "assistant-message"}`;

  if (role !== "user") {
    const avatar = document.createElement("span");
    avatar.className = "message-avatar";
    avatar.setAttribute("aria-hidden", "true");
    avatar.textContent = "M";
    wrapper.append(avatar);
  }

  const content = document.createElement("div");
  content.className = "message-content";
  const author = document.createElement("span");
  author.className = "message-author";
  author.textContent = role === "user" ? "You" : "MediVoice";
  const time = document.createElement("time");
  time.textContent = new Date().toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  author.append(time);

  const paragraph = document.createElement("p");
  paragraph.textContent = text;
  if (isError) paragraph.classList.add("error-message");

  content.append(author, paragraph);
  wrapper.append(content);
  conversation.append(wrapper);
  conversation.scrollTop = conversation.scrollHeight;
}

function formatAppointmentTime(value) {
  return new Date(value).toLocaleString([], {
    weekday: "short",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

function renderBooking(booking) {
  currentBooking = booking;
  bookingContent.replaceChildren();

  const card = document.createElement("div");
  card.className = "booking-card";
  const top = document.createElement("div");
  top.className = "booking-card-top";
  const name = document.createElement("h3");
  name.textContent = booking.doctor_name || booking.doctor || "Your doctor";
  const badge = document.createElement("span");
  badge.className = "confirmed-badge";
  badge.textContent = "CONFIRMED";
  top.append(name, badge);

  const meta = document.createElement("p");
  meta.className = "booking-meta";
  const speciality = booking.speciality ? `${booking.speciality} · ` : "";
  const phone = booking.patient_phone ? `Phone ending ${booking.patient_phone.slice(-4)}` : "";
  meta.textContent = `${speciality}${formatAppointmentTime(booking.start_time)}${phone ? `\n${phone}` : ""}`;

  const cancel = document.createElement("button");
  cancel.className = "cancel-button";
  cancel.type = "button";
  cancel.textContent = "Cancel appointment";
  cancel.addEventListener("click", cancelCurrentBooking);

  card.append(top, meta, cancel);
  bookingContent.append(card);
  document.querySelector("#booking-panel").scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function clearBooking() {
  currentBooking = null;
  const empty = document.createElement("div");
  empty.className = "booking-empty";
  const icon = document.createElement("span");
  icon.className = "empty-icon";
  icon.setAttribute("aria-hidden", "true");
  icon.textContent = "✳";
  const text = document.createElement("p");
  text.textContent = "Your confirmed appointment will appear here.";
  empty.append(icon, text);
  bookingContent.replaceChildren(empty);
}

function applyAssistantActions(actions = []) {
  for (const action of actions) {
    const result = action.result;
    if (!result?.success) continue;
    if (action.name === "book_appointment") {
      renderBooking(result);
      showToast("Your appointment is confirmed.");
      void availabilityForm.requestSubmit();
    } else if (action.name === "cancel_appointment") {
      if (currentBooking?.appointment_id === result.appointment_id) {
        clearBooking();
        showToast("Your appointment was cancelled.");
        void availabilityForm.requestSubmit();
      }
    }
  }
}

function speak(text) {
  if (!("speechSynthesis" in window) || !text) return;
  window.speechSynthesis.cancel();
  const utterance = new SpeechSynthesisUtterance(text);
  utterance.rate = 1;
  window.speechSynthesis.speak(utterance);
}

async function sendMessage(text) {
  const message = text.trim();
  if (!message) return;
  chatInput.value = "";
  appendMessage("user", message);
  history.push({ role: "user", content: message });

  const submitButton = chatForm.querySelector('button[type="submit"]');
  submitButton.disabled = true;
  chatInput.disabled = true;
  setVoiceStatus("MediVoice is thinking...");

  try {
    const response = await fetch("/agent/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        messages: history.slice(-20),
        pending_booking_id: pendingBookingId,
        pending_cancellation_id: pendingCancellationId,
      }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "The assistant is unavailable right now.");

    if (Object.prototype.hasOwnProperty.call(data, "pending_booking_id")) {
      pendingBookingId = data.pending_booking_id;
      if (pendingBookingId) pendingCancellationId = null;
    }
    if (Object.prototype.hasOwnProperty.call(data, "pending_cancellation_id")) {
      pendingCancellationId = data.pending_cancellation_id;
      if (pendingCancellationId) pendingBookingId = null;
    }
    history.push({ role: "assistant", content: data.reply });
    appendMessage("assistant", data.reply);
    applyAssistantActions(data.actions);
    speak(data.reply);
  } catch (error) {
    appendMessage("assistant", error.message || "Something went wrong. Please try again.", true);
    showToast(error.message || "The assistant is unavailable.");
  } finally {
    submitButton.disabled = false;
    chatInput.disabled = false;
    chatInput.focus();
    setVoiceStatus("Your microphone is off.");
  }
}

chatForm.addEventListener("submit", (event) => {
  event.preventDefault();
  void sendMessage(chatInput.value);
});

async function loadAvailability(event) {
  event.preventDefault();
  const speciality = document.querySelector("#speciality").value;
  const date = document.querySelector("#appointment-date").value;
  const submit = availabilityForm.querySelector('button[type="submit"]');
  results.replaceChildren();
  submit.disabled = true;
  const loading = document.createElement("p");
  loading.className = "inline-state";
  loading.textContent = "Checking the schedule...";
  results.append(loading);

  try {
    const query = new URLSearchParams({ speciality, date });
    const response = await fetch(`/doctors/availability?${query}`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Could not load appointment times.");
    results.replaceChildren();
    const doctors = data.doctors || [];
    if (!doctors.length) {
      const empty = document.createElement("p");
      empty.className = "inline-state";
      empty.textContent = "No open times for this speciality on that date. Try another date.";
      results.append(empty);
      return;
    }

    for (const doctor of doctors) {
      const card = document.createElement("div");
      card.className = "doctor-result";
      const name = document.createElement("h3");
      name.textContent = doctor.doctor_name;
      const detail = document.createElement("p");
      detail.textContent = doctor.speciality;
      const slotList = document.createElement("div");
      slotList.className = "slot-list";

      for (const slot of doctor.available_slots) {
        const button = document.createElement("button");
        button.className = "slot-button";
        button.type = "button";
        button.textContent = new Date(slot.start_time).toLocaleTimeString([], {
          hour: "numeric",
          minute: "2-digit",
        });
        button.addEventListener("click", () => bookSelectedSlot(doctor, slot));
        slotList.append(button);
      }

      card.append(name, detail, slotList);
      results.append(card);
    }
  } catch (error) {
    results.replaceChildren();
    const failure = document.createElement("p");
    failure.className = "inline-state error";
    failure.textContent = error.message || "Could not load appointment times.";
    results.append(failure);
  } finally {
    submit.disabled = false;
  }
}

availabilityForm.addEventListener("submit", (event) => void loadAvailability(event));

async function bookSelectedSlot(doctor, slot) {
  if (appointmentMutationInProgress) return;
  const patientPhone = phoneInput.value.trim();
  if (!patientPhone) {
    phoneInput.focus();
    showToast("Enter a phone number before booking.");
    return;
  }

  const bookingDetails = `Book ${doctor.doctor_name} for ${formatAppointmentTime(slot.start_time)} using phone number ${patientPhone}?`;
  if (!window.confirm(`${bookingDetails}\n\nChoose OK only if all details are correct.`)) return;

  appointmentMutationInProgress = true;
  try {
    const response = await fetch("/appointments/book", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        doctor_id: doctor.doctor_id,
        slot_id: slot.slot_id,
        patient_phone: patientPhone,
      }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Booking could not be completed.");
    renderBooking(data);
    showToast("Your appointment is confirmed.");
    const confirmation = `You're booked with ${data.doctor_name} for ${formatAppointmentTime(data.start_time)}. Appointment ID ${data.appointment_id}.`;
    history.push({ role: "assistant", content: confirmation });
    appendMessage("assistant", confirmation);
    void availabilityForm.requestSubmit();
  } catch (error) {
    showToast(error.message || "Booking could not be completed.");
    if (error.message.includes("no longer available")) {
      void availabilityForm.requestSubmit();
    }
  } finally {
    appointmentMutationInProgress = false;
  }
}

async function cancelCurrentBooking() {
  if (!currentBooking || appointmentMutationInProgress) return;
  const cancellationDetails = `Cancel appointment ${currentBooking.appointment_id} with ${currentBooking.doctor_name || currentBooking.doctor} using phone number ${currentBooking.patient_phone || phoneInput.value.trim()}?`;
  if (!window.confirm(`${cancellationDetails}\n\nChoose OK to confirm cancellation.`)) return;
  appointmentMutationInProgress = true;
  try {
    const response = await fetch(`/appointments/${currentBooking.appointment_id}/cancel`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ patient_phone: currentBooking.patient_phone || phoneInput.value.trim() }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Cancellation could not be completed.");
    clearBooking();
    showToast("Your appointment was cancelled.");
    const confirmation = `Appointment ${data.appointment_id} has been cancelled.`;
    history.push({ role: "assistant", content: confirmation });
    appendMessage("assistant", confirmation);
    void availabilityForm.requestSubmit();
  } catch (error) {
    showToast(error.message || "Cancellation could not be completed.");
  } finally {
    appointmentMutationInProgress = false;
  }
}

function startBrowserRecognition() {
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  recognition = new Recognition();
  recognition.lang = navigator.language || "en-US";
  recognition.interimResults = true;
  recognition.continuous = false;

  recognition.onstart = () => setVoiceStatus("Listening... speak now.", true);
  recognition.onresult = (event) => {
    let finalText = "";
    let interimText = "";
    for (let index = event.resultIndex; index < event.results.length; index += 1) {
      const transcript = event.results[index][0].transcript;
      if (event.results[index].isFinal) finalText += transcript;
      else interimText += transcript;
    }
    if (interimText) setVoiceStatus(`Listening: ${interimText}`, true);
    if (finalText.trim()) {
      setVoiceStatus(`You said: ${finalText.trim()}`);
      void sendMessage(finalText);
    }
  };
  recognition.onerror = (event) => {
    if (event.error !== "no-speech" && event.error !== "aborted") {
      setVoiceStatus("Microphone recognition failed. Check permission and try again.");
      showToast("Could not recognize speech. You can type instead.");
    }
  };
  recognition.onend = () => {
    recognition = null;
    if (voiceStatus.textContent.startsWith("Listening")) setVoiceStatus("Your microphone is off.");
    micButton.classList.remove("is-recording");
  };
  recognition.start();
}

async function startRecordedRecognition() {
  if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
    throw new Error("This browser does not support microphone recording.");
  }
  audioStream = await navigator.mediaDevices.getUserMedia({ audio: true });
  audioChunks = [];
  mediaRecorder = new MediaRecorder(audioStream);
  mediaRecorder.ondataavailable = (event) => {
    if (event.data.size) audioChunks.push(event.data);
  };
  mediaRecorder.onstop = async () => {
    const audio = new Blob(audioChunks, { type: mediaRecorder.mimeType || "audio/webm" });
    audioStream.getTracks().forEach((track) => track.stop());
    audioStream = null;
    mediaRecorder = null;
    setVoiceStatus("Transcribing your recording...");
    try {
      const formData = new FormData();
      formData.append("audio", audio, "recording.webm");
      const response = await fetch("/voice/transcribe", { method: "POST", body: formData });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Speech transcription failed.");
      if (!data.text) throw new Error("No speech was detected. Please try again.");
      setVoiceStatus(`You said: ${data.text}`);
      void sendMessage(data.text);
    } catch (error) {
      setVoiceStatus(error.message || "Speech transcription failed.");
      showToast(error.message || "Speech transcription failed.");
    }
  };
  mediaRecorder.start();
  setVoiceStatus("Recording... tap the mic again when you're done.", true);
}

micButton.addEventListener("click", async () => {
  if (recognition) {
    recognition.stop();
    return;
  }
  if (mediaRecorder?.state === "recording") {
    mediaRecorder.stop();
    return;
  }

  try {
    const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (Recognition) startBrowserRecognition();
    else await startRecordedRecognition();
  } catch (error) {
    setVoiceStatus(error.message || "Microphone permission is unavailable.");
    showToast(error.message || "Allow microphone access to use voice input.");
  }
});

if (!("speechSynthesis" in window)) {
  setVoiceStatus("Text-to-speech is not supported in this browser; messages remain available as text.");
}
