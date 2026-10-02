# VideoBoost
Painel web para upload em massa, fila de vídeos, downloads individuais e em ZIP, edição em fila e integração social via OAuth.

## Deploy
- Runtime: Python
- Build: pip install -r requirements.txt
- Start: uvicorn main:app --host 0.0.0.0 --port $PORT

## Variáveis opcionais
SECRET_KEY
DATABASE_URL
META_CLIENT_ID
META_CLIENT_SECRET
META_REDIRECT_URI

A integração real com Instagram/Meta exige um aplicativo configurado no Meta for Developers e permissões aprovadas. O processamento de edição de vídeo pode ser conectado a FFmpeg posteriormente.
