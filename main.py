import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from fastapi.responses import FileResponse

from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine, String, Integer, DateTime, Text, select, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, Session, sessionmaker
from pwdlib import PasswordHash
import jwt

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./videoboost.db")
SECRET_KEY = os.getenv("SECRET_KEY", "CHANGE-ME-IN-RENDER")
ALGORITHM = "HS256"
TOKEN_DAYS = 7
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", "./uploads"))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

class Base(DeclarativeBase):
    pass

class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    filename: Mapped[str] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(30), default="pending")
    progress: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

Base.metadata.create_all(engine)
password_hash = PasswordHash.recommended()

app = FastAPI(title="VideoBoost API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()

def make_token(user_id: int) -> str:
    exp = datetime.now(timezone.utc) + timedelta(days=TOKEN_DAYS)
    return jwt.encode({"sub": str(user_id), "exp": exp}, SECRET_KEY, algorithm=ALGORITHM)

def current_user(authorization: str | None = None, db: Session = Depends(db)) -> User:
    # FastAPI Header dependency is added below by wrapper; kept separate for clarity.
    raise HTTPException(status_code=401, detail="Não autenticado")

from fastapi import Header

def get_current_user(
    authorization: str | None = Header(default=None),
    session: Session = Depends(db),
) -> User:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Não autenticado")
    token = authorization[7:].strip()
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = int(payload["sub"])
    except Exception:
        raise HTTPException(status_code=401, detail="Token inválido ou expirado")
    user = session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=401, detail="Usuário não encontrado")
    return user

@app.get("/")
def root():
    return FileResponse("index.html")


@app.get("/login.html")
def login_page():
    return FileResponse("login.html")


@app.get("/dashboard.html")
def dashboard_page():
    return FileResponse(Path(__file__).resolve().parent / "painel.html")
@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/api/auth/register")
def register(
    email: str = Form(...),
    password: str = Form(...),
    session: Session = Depends(db),
):
    email = email.strip().lower()
    if len(password) < 6:
        raise HTTPException(status_code=400, detail="A senha precisa ter pelo menos 6 caracteres.")
    existing = session.scalar(select(User).where(User.email == email))
    if existing:
        raise HTTPException(status_code=409, detail="Este e-mail já possui uma conta.")
    user = User(email=email, password_hash=password_hash.hash(password))
    session.add(user)
    session.commit()
    session.refresh(user)
    return {"token": make_token(user.id), "user": {"id": user.id, "email": user.email}}

@app.post("/api/auth/login")
def login(
    email: str = Form(...),
    password: str = Form(...),
    session: Session = Depends(db),
):
    email = email.strip().lower()
    user = session.scalar(select(User).where(User.email == email))
    if not user or not password_hash.verify(password, user.password_hash):
        raise HTTPException(status_code=401, detail="E-mail ou senha incorretos.")
    return {"token": make_token(user.id), "user": {"id": user.id, "email": user.email}}

@app.get("/api/jobs")
def list_jobs(user: User = Depends(get_current_user), session: Session = Depends(db)):
    jobs = session.scalars(
        select(Job).where(Job.user_id == user.id).order_by(Job.created_at.desc())
    ).all()
    counts = {}
    for status in ("pending", "running", "done", "error"):
        counts[status] = session.scalar(
            select(func.count()).select_from(Job).where(Job.user_id == user.id, Job.status == status)
        ) or 0
    return {
        "jobs": [
            {"id": j.id, "filename": j.filename, "status": j.status, "progress": j.progress}
            for j in jobs
        ],
        "stats": {
            "pending": counts["pending"],
            "running": counts["running"],
            "done": counts["done"],
            "errors": counts["error"],
        },
    }

@app.post("/api/jobs")
async def create_jobs(
    files: list[UploadFile] = File(...),
    format: str = Form("vertical"),
    priority: str = Form("normal"),
    text: str = Form(""),
    text_position: str = Form("bottom"),
    opacity: str = Form("0.85"),
    user: User = Depends(get_current_user),
    session: Session = Depends(db),
):
    created = []
    for upload in files:
        safe_name = Path(upload.filename or "video.mp4").name
        job_id = str(uuid.uuid4())
        target = UPLOAD_DIR / f"{job_id}_{safe_name}"
        with target.open("wb") as out:
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
        job = Job(id=job_id, user_id=user.id, filename=safe_name, status="pending", progress=0)
        session.add(job)
        created.append({"id": job_id, "filename": safe_name, "status": "pending"})
    session.commit()
    return {"created": created, "message": "Vídeo(s) recebido(s) e colocado(s) na fila."}

@app.get("/api/operations/overview")
def overview(user: User = Depends(get_current_user), session: Session = Depends(db)):
    scheduled = 0
    done = session.scalar(
        select(func.count()).select_from(Job).where(Job.user_id == user.id, Job.status == "done")
    ) or 0
    return {
        "metrics": {
            "videos_done": done,
            "publications_scheduled": scheduled,
        }
    }
