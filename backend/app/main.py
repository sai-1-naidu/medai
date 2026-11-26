from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.database import Base, engine
from app.routes.appointments import router as appointments_router
from app.routes.chat import router as chat_router
from app.routes.doctors import router as doctors_router
from app.routes.voice import router as voice_router
from app.seed import seed_database

FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(bind=engine)
    seed_database()
    yield


app = FastAPI(
    title="MediVoice AI",
    description="A voice-enabled clinic receptionist demo for finding doctors and managing appointments.",
    version="1.0.0",
    lifespan=lifespan,
)

app.mount("/assets", StaticFiles(directory=FRONTEND_DIR / "assets"), name="assets")
app.include_router(doctors_router)
app.include_router(appointments_router)
app.include_router(chat_router)
app.include_router(voice_router)


@app.get("/", include_in_schema=False)
def dashboard():
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/health")
def health():
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
    return {"status": "healthy"}
