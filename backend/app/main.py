from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.database import engine, Base

from app import models

from app.routes.doctors import router as doctors_router
from app.routes.appointments import router as appointments_router
Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="MediVoice AI",
    description="MediVoice AI is a cutting-edge application that leverages advanced artificial intelligence to provide accurate and efficient medical voice recognition and transcription services. Our platform is designed to assist healthcare professionals in documenting patient interactions, streamlining workflows, and enhancing overall productivity. With MediVoice AI, users can expect high-quality transcriptions, seamless integration with existing systems, and a user-friendly interface that prioritizes ease of use and accessibility.",
    version="1.0.0",
)



app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(doctors_router)
app.include_router(appointments_router)

@app.get("/")
def root():
    return {"message": "Welcome to MediVoice AI!"}

@app.get("/health")
def health():
    return {"status": "healthy"}