import os
import uuid
import shutil
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Form, Header, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy import create_engine, String, Integer, DateTime, Text, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, Session, sessionmaker
from pwdlib import PasswordHash
import jwt

BASE = Path(__file__).resolve().parent
UPLOAD_DIR = BASE / "uploads"
OUTPUT_DIR = BASE / "outputs"
UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./videoboost.db")
SECRET_KEY = os.getenv("SECRET_KEY", "CHANGE-ME-IN-RENDER")
ALGORITHM = "HS256"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

password_hash = PasswordHash.recommended()

class Base(DeclarativeBase):
    pass

class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password: Mapped[str] = mapped_column(String(500))

class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    filename: Mapped[str] = mapped_column(String(500))
    original_path: Mapped[str] = mapped_column(Text)
    output_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(50), default="pending")
    progress: Mapped[int] = mapped_column(Integer, default=0)
    operation: Mapped[str] = mapped_column(String(100), default="upload")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

Base.metadata.create_all(engine)

app = FastAPI(title="VideoBoost API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()

def make_token(user_id: int):
    payload = {
        "sub": str(user_id),
        "exp": datetime.now(timezone.utc) + timedelta(days=7),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)

def current_user(
    authorization: str = Header(default=""),
    session: Session = Depends(db),
):
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "Não autenticado.")
    token = authorization.split(" ", 1)[1]
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = int(payload["sub"])
    except Exception:
        raise HTTPException(401, "Token inválido ou expirado.")
    user = session.get(User, user_id)
    if not user:
        raise HTTPException(401, "Usuário não encontrado.")
    return user

@app.get("/")
def home():
    return RedirectResponse("/dashboard.html")

@app.get("/index.html")
def index_page():
    path = BASE / "index.html"
    if path.exists():
        return FileResponse(path)
    return RedirectResponse("/dashboard.html")

@app.get("/login.html")
def login_page():
    path = BASE / "login.html"
    if path.exists():
        return FileResponse(path)
    return RedirectResponse("/dashboard.html")

@app.get("/painel.html")
def painel_page():
    return FileResponse(BASE / "painel.html")

@app.get("/dashboard.html")
def dashboard_page():
    return FileResponse(BASE / "dashboard.html")

@app.post("/api/register")
def register(email: str = Form(...), password: str = Form(...), session: Session = Depends(db)):
    email = email.strip().lower()
    if not email or len(password) < 4:
        raise HTTPException(400, "E-mail e senha são obrigatórios.")
    if session.scalar(select(User).where(User.email == email)):
        raise HTTPException(409, "E-mail já cadastrado.")
    user = User(email=email, password=password_hash.hash(password))
    session.add(user)
    session.commit()
    session.refresh(user)
    return {"token": make_token(user.id), "user": {"id": user.id, "email": user.email}}

@app.post("/api/login")
def login(email: str = Form(...), password: str = Form(...), session: Session = Depends(db)):
    email = email.strip().lower()
    user = session.scalar(select(User).where(User.email == email))
    if not user or not password_hash.verify(password, user.password):
        raise HTTPException(401, "E-mail ou senha inválidos.")
    return {"token": make_token(user.id), "user": {"id": user.id, "email": user.email}}

def process_job(job_id: str):
    session = SessionLocal()
    job = None
    try:
        job = session.get(Job, job_id)
        if not job:
            return

        source = Path(job.original_path)
        if not source.exists():
            job.status = "error"
            job.progress = 0
            job.updated_at = datetime.now(timezone.utc)
            session.commit()
            return

        job.status = "processing"
        job.progress = 10
        job.updated_at = datetime.now(timezone.utc)
        session.commit()

        output = OUTPUT_DIR / f"{job.id}_{job.filename}"
        output.parent.mkdir(exist_ok=True)
        shutil.copyfile(source, output)

        job.progress = 70
        job.updated_at = datetime.now(timezone.utc)
        session.commit()

        job.output_path = str(output)
        job.status = "done"
        job.progress = 100
        job.updated_at = datetime.now(timezone.utc)
        session.commit()

    except Exception:
        session.rollback()
        if job:
            job = session.get(Job, job_id)
            if job:
                job.status = "error"
                job.updated_at = datetime.now(timezone.utc)
                session.commit()
    finally:
        session.close()

@app.post("/api/jobs")
async def create_jobs(
    background_tasks: BackgroundTasks,
    files: list[UploadFile] = File(...),
    format: str = Form("original"),
    priority: str = Form("normal"),
    text: str = Form(""),
    user=Depends(current_user),
    session: Session = Depends(db),
):
    if not files:
        raise HTTPException(400, "Nenhum vídeo foi enviado.")

    created = []

    for upload in files:
        name = Path(upload.filename or "video.mp4").name or "video.mp4"
        job_id = str(uuid.uuid4())
        target = UPLOAD_DIR / f"{job_id}_{name}"

        try:
            with target.open("wb") as output:
                while True:
                    chunk = await upload.read(1024 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)
        finally:
            await upload.close()

        job = Job(
            id=job_id,
            user_id=user.id,
            filename=name,
            original_path=str(target),
            status="pending",
            progress=0,
            operation="upload",
        )
        session.add(job)
        created.append({"id": job_id, "filename": name, "status": "pending"})

    session.commit()

    for item in created:
        background_tasks.add_task(process_job, item["id"])

    return {
        "created": created,
        "message": "Vídeo(s) recebido(s) e colocado(s) na fila.",
    }

@app.get("/api/jobs")
def jobs(user=Depends(current_user), session: Session = Depends(db)):
    rows = session.scalars(
        select(Job)
        .where(Job.user_id == user.id)
        .order_by(Job.created_at.desc())
    ).all()

    return {
        "stats": {
            "pending": sum(x.status in ("pending", "processing") for x in rows),
            "done": sum(x.status == "done" for x in rows),
            "total": len(rows),
        },
        "jobs": [
            {
                "id": x.id,
                "filename": x.filename,
                "status": x.status,
                "progress": x.progress,
                "output_path": x.output_path,
            }
            for x in rows
        ],
    }

@app.get("/api/jobs/{job_id}/download")
def download(
    job_id: str,
    token: str | None = None,
    authorization: str = Header(default=""),
    session: Session = Depends(db),
):
    if token:
        authorization = "Bearer " + token

    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "Não autenticado.")

    try:
        payload = jwt.decode(authorization.split(" ", 1)[1], SECRET_KEY, algorithms=[ALGORITHM])
        user_id = int(payload["sub"])
    except Exception:
        raise HTTPException(401, "Token inválido.")

    job = session.get(Job, job_id)
    if not job or job.user_id != user_id:
        raise HTTPException(404, "Vídeo não encontrado.")

    path = Path(job.output_path or job.original_path)
    if not path.exists():
        raise HTTPException(404, "Arquivo não encontrado.")

    return FileResponse(path, filename=job.filename, media_type="application/octet-stream")

@app.post("/api/downloads/zip")
def zip_download(user=Depends(current_user), session: Session = Depends(db)):
    rows = session.scalars(select(Job).where(Job.user_id == user.id)).all()
    valid = [
        job for job in rows
        if job.status == "done" and job.output_path and Path(job.output_path).exists()
    ]

    if not valid:
        raise HTTPException(404, "Nenhum vídeo concluído para baixar.")

    zip_path = OUTPUT_DIR / f"{user.id}_downloads.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for job in valid:
            archive.write(job.output_path, arcname=job.filename)

    return FileResponse(zip_path, filename="videoboost_videos.zip", media_type="application/zip")

@app.get("/api/health")
def health():
    return {"status": "ok", "service": "VideoBoost"}

@app.get("/api/social/meta/login")
def meta_login():
    client_id = os.getenv("META_CLIENT_ID", "")
    if not client_id:
        raise HTTPException(503, "Integração Meta/Instagram ainda não configurada.")
    redirect_uri = os.getenv(
        "META_REDIRECT_URI",
        "https://videoboost-backend.onrender.com/api/social/meta/callback",
    )
    scopes = "instagram_basic,instagram_content_publish,pages_show_list,pages_read_engagement"
    url = (
        "https://www.facebook.com/v24.0/dialog/oauth?"
        f"client_id={client_id}&redirect_uri={redirect_uri}&scope={scopes}"
    )
    return RedirectResponse(url)

@app.get("/api/social/meta/callback")
def meta_callback():
    return {"message": "Callback recebido. A integração Meta/Instagram será concluída na próxima etapa."}
