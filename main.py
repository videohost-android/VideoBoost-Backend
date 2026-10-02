import os, uuid, shutil, zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Form, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy import create_engine, String, Integer, DateTime, Text, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, Session, sessionmaker
from pwdlib import PasswordHash
import jwt

BASE=Path(__file__).resolve().parent
UPLOAD_DIR=BASE/'uploads'; OUTPUT_DIR=BASE/'outputs'
UPLOAD_DIR.mkdir(exist_ok=True); OUTPUT_DIR.mkdir(exist_ok=True)
DATABASE_URL=os.getenv('DATABASE_URL','sqlite:///./videoboost.db')
SECRET_KEY=os.getenv('SECRET_KEY','CHANGE-ME-IN-RENDER')
ALGORITHM='HS256'
engine=create_engine(DATABASE_URL,connect_args={'check_same_thread':False} if DATABASE_URL.startswith('sqlite') else {})
SessionLocal=sessionmaker(bind=engine,autocommit=False,autoflush=False)
ph=PasswordHash.recommended()
class Base(DeclarativeBase): pass
class User(Base):
    __tablename__='users'; id:Mapped[int]=mapped_column(Integer,primary_key=True); email:Mapped[str]=mapped_column(String(255),unique=True,index=True); password_hash:Mapped[str]=mapped_column(String(500)); created_at:Mapped[datetime]=mapped_column(DateTime,default=lambda:datetime.now(timezone.utc))
class Job(Base):
    __tablename__='jobs'; id:Mapped[str]=mapped_column(String(100),primary_key=True); user_id:Mapped[int]=mapped_column(Integer,index=True); filename:Mapped[str]=mapped_column(String(500)); original_path:Mapped[str]=mapped_column(String(1000)); output_path:Mapped[str|None]=mapped_column(String(1000),nullable=True); status:Mapped[str]=mapped_column(String(50),default='pending'); progress:Mapped[int]=mapped_column(Integer,default=0); operation:Mapped[str]=mapped_column(String(50),default='upload'); created_at:Mapped[datetime]=mapped_column(DateTime,default=lambda:datetime.now(timezone.utc)); updated_at:Mapped[datetime]=mapped_column(DateTime,default=lambda:datetime.now(timezone.utc))
Base.metadata.create_all(engine)
app=FastAPI(title='VideoBoost API',version='2.1.0')
app.add_middleware(CORSMiddleware,allow_origins=['*'],allow_credentials=True,allow_methods=['*'],allow_headers=['*'])
def db():
    s=SessionLocal()
    try: yield s
    finally: s.close()
def token_for(user):
    return jwt.encode({'sub':str(user.id),'exp':datetime.now(timezone.utc)+timedelta(days=7)},SECRET_KEY,algorithm=ALGORITHM)
def current_user(authorization:str=Header(default=''),session:Session=Depends(db)):
    if not authorization.startswith('Bearer '): raise HTTPException(401,'Sessão necessária')
    try: uid=int(jwt.decode(authorization[7:],SECRET_KEY,algorithms=[ALGORITHM])['sub'])
    except Exception: raise HTTPException(401,'Token inválido ou expirado')
    user=session.get(User,uid)
    if not user: raise HTTPException(401,'Usuário não encontrado')
    return user
@app.get('/')
def root(): return RedirectResponse('/login.html')
@app.get('/login.html')
def login_page(): return FileResponse(BASE/'login.html')
@app.get('/dashboard.html')
def dashboard_page(): return FileResponse(BASE/'dashboard.html')
@app.get('/health')
def health(): return {'status':'ok','version':'2.1.0'}
@app.post('/api/auth/register')
def register(data:dict,session:Session=Depends(db)):
    email=data.get('email','').strip().lower(); password=data.get('password','')
    if not email or len(password)<6: raise HTTPException(400,'Informe e-mail e senha com pelo menos 6 caracteres.')
    if session.scalar(select(User).where(User.email==email)): raise HTTPException(409,'E-mail já cadastrado.')
    u=User(email=email,password_hash=ph.hash(password)); session.add(u); session.commit(); session.refresh(u)
    return {'access_token':token_for(u),'user':{'id':u.id,'email':u.email}}
@app.post('/api/auth/login')
def login(data:dict,session:Session=Depends(db)):
    email=data.get('email','').strip().lower(); u=session.scalar(select(User).where(User.email==email))
    if not u or not ph.verify(u.password_hash,data.get('password','')): raise HTTPException(401,'E-mail ou senha inválidos.')
    return {'access_token':token_for(u),'user':{'id':u.id,'email':u.email}}
@app.get('/api/me')
def me(user=Depends(current_user)): return {'id':user.id,'email':user.email}
@app.post('/api/jobs')
async def create_jobs(files:list[UploadFile]=File(...),format:str=Form('original'),priority:str=Form('normal'),text:str=Form(''),user=Depends(current_user),session:Session=Depends(db)):
    if not files: raise HTTPException(400,'Nenhum vídeo foi enviado.')
    created=[]
    for upload in files:
        name=Path(upload.filename or 'video.mp4').name or 'video.mp4'; jid=str(uuid.uuid4()); target=UPLOAD_DIR/f'{jid}_{name}'
        with target.open('wb') as out:
            while True:
                chunk=await upload.read(1024*1024)
                if not chunk: break
                out.write(chunk)
        await upload.close()
        j=Job(id=jid,user_id=user.id,filename=name,original_path=str(target),status='pending',progress=0,operation='upload'); session.add(j); created.append({'id':jid,'filename':name,'status':'pending'})
    session.commit()
    return {'created':created,'message':'Vídeo(s) recebido(s) e colocado(s) na fila.'}
@app.get('/api/jobs')
def jobs(user=Depends(current_user),session:Session=Depends(db)):
    rows=session.scalars(select(Job).where(Job.user_id==user.id).order_by(Job.created_at.desc())).all()
    return {'stats':{'pending':sum(x.status in ('pending','processing') for x in rows),'done':sum(x.status=='done' for x in rows),'total':len(rows)},'jobs':[{'id':x.id,'filename':x.filename,'status':x.status,'progress':x.progress,'output_path':x.output_path} for x in rows]}
@app.get('/api/jobs/{job_id}/download')
def download(job_id:str,token:str|None=None,authorization:str=Header(default=''),session:Session=Depends(db)):
    if token: authorization='Bearer '+token
    user=current_user(authorization,session); j=session.get(Job,job_id)
    if not j or j.user_id!=user.id: raise HTTPException(404,'Vídeo não encontrado')
    path=Path(j.output_path or j.original_path)
    if not path.exists(): raise HTTPException(404,'Arquivo não encontrado')
    return FileResponse(path,filename=j.filename,media_type='application/octet-stream')
@app.post('/api/downloads/zip')
def zip_download(user=Depends(current_user),session:Session=Depends(db)):
    rows=session.scalars(select(Job).where(Job.user_id==user.id)).all(); valid=[x for x in rows if Path(x.output_path or x.original_path).exists()]
    if not valid: raise HTTPException(404,'Nenhum vídeo disponível')
    p=OUTPUT_DIR/f'{user.id}_downloads.zip'
    with zipfile.ZipFile(p,'w',zipfile.ZIP_DEFLATED) as z:
        for j in valid: z.write(j.output_path or j.original_path,arcname=j.filename)
    return FileResponse(p,filename='videoboost_videos.zip',media_type='application/zip')
@app.post('/api/jobs/{job_id}/edit')
def edit_job(job_id:str,user=Depends(current_user),session:Session=Depends(db)):
    j=session.get(Job,job_id)
    if not j or j.user_id!=user.id: raise HTTPException(404,'Vídeo não encontrado')
    j.operation='edit'; j.status='pending'; j.updated_at=datetime.now(timezone.utc); session.commit()
    return {'message':'Edição colocada na fila. O processamento FFmpeg pode ser conectado nesta etapa.'}
META_CLIENT_ID=os.getenv('META_CLIENT_ID',''); META_CLIENT_SECRET=os.getenv('META_CLIENT_SECRET',''); META_REDIRECT_URI=os.getenv('META_REDIRECT_URI','https://videoboost-backend.onrender.com/api/social/meta/callback')
@app.get('/api/social/meta/login')
def meta_login(user=Depends(current_user)):
    if not META_CLIENT_ID: raise HTTPException(503,'Integração Meta/Instagram ainda não configurada no Render.')
    params={'client_id':META_CLIENT_ID,'redirect_uri':META_REDIRECT_URI,'response_type':'code','scope':'instagram_basic,instagram_content_publish,pages_show_list,pages_read_engagement'}
    return {'url':'https://www.facebook.com/v24.0/dialog/oauth?'+urlencode(params)}
@app.get('/api/social/meta/callback')
def meta_callback(code:str|None=None,error:str|None=None):
    if error: return RedirectResponse('/dashboard.html?social_error=1')
    return RedirectResponse('/dashboard.html?social_connected=1')
@app.get('/api/social/accounts')
def social_accounts(user=Depends(current_user)): return {'accounts':[]}
