# VideoBoost Backend

API FastAPI para o site VideoBoost.

## Endpoints principais

- `GET /health`
- `POST /api/auth/register`
- `POST /api/auth/login`
- `GET /api/jobs`
- `POST /api/jobs`
- `GET /api/operations/overview`

## Rodar localmente

```bash
pip install -r requirements.txt
uvicorn main:app --reload
```

Depois abra `/docs`.

## Render

Build:
`pip install -r requirements.txt`

Start:
`uvicorn main:app --host 0.0.0.0 --port $PORT`

### Banco de dados
Sem `DATABASE_URL`, o projeto usa SQLite localmente. Em Render Free, o disco local é efêmero; para dados de contas que precisam sobreviver a reinícios/redeploys, configure um PostgreSQL e defina `DATABASE_URL`.

### Segurança
Defina `SECRET_KEY` como um segredo forte em produção. Nunca coloque a chave diretamente no código.
