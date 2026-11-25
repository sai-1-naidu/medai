from groq import Groq

from app.config import GROQ_API_KEY, GROQ_MODEL


if not GROQ_API_KEY:
    raise RuntimeError("GROQ_API_KEY is missing from backend/.env")


client = Groq(api_key=GROQ_API_KEY)


response = client.chat.completions.create(
    model=GROQ_MODEL,
    messages=[
        {
            "role": "system",
            "content": (
                "You are MediVoice AI, a polite medical receptionist. "
                "You help patients with doctor availability and appointments. "
                "Never diagnose medical conditions."
            ),
        },
        {
            "role": "user",
            "content": "Hello, I want to book an appointment.",
        },
    ],
)


answer = response.choices[0].message.content

print("\nMediVoice AI response:")
print(answer)