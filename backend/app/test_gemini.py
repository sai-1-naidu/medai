from google import genai

from app.config import GEMINI_API_KEY, GEMINI_MODEL


if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY is missing from backend/.env")


client = genai.Client(
    api_key=GEMINI_API_KEY
)


response = client.models.generate_content(
    model=GEMINI_MODEL,
    contents="Reply with exactly: MediVoice AI connection successful."
)


print("Gemini response:")
print(response.text)