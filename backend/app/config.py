import os
from dotenv import load_dotenv

load_dotenv()

#GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "JBFqnCBsd6RMkjVDRZzb")

#GEMINI_MODEL = "gemini-3.8-flash"

# Active development LLM
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")


def validate_api_keys():
    missing_keys = []

    if not ELEVENLABS_API_KEY:
        missing_keys.append("ELEVENLABS_API_KEY")

    if not GROQ_API_KEY:
        missing_keys.append("GROQ_API_KEY")

    if missing_keys:
        raise RuntimeError(
            f"Missing environment variables: {', '.join(missing_keys)}"
        )