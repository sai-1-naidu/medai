import os

from dotenv import load_dotenv
from elevenlabs.client import ElevenLabs


load_dotenv()

api_key = os.getenv("ELEVENLABS_API_KEY")

if not api_key:
    raise RuntimeError("ELEVENLABS_API_KEY is missing from backend/.env")


client = ElevenLabs(
    api_key=api_key
)


audio = client.text_to_speech.convert(
    text="Welcome to MediVoice AI. I can help you find a doctor and book an appointment.",
    voice_id="JBFqnCBsd6RMkjVDRZzb",
    model_id="eleven_v3",
    output_format="mp3_44100_128"
)


output_path = "medivoice_test.mp3"

with open(output_path, "wb") as audio_file:
    for chunk in audio:
        audio_file.write(chunk)


print(f"Audio generated successfully: {output_path}")