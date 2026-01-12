# Deployment Guide (v0.9.0)

## Local Development (Codespaces/VSCode)

1. **Clone & Install**:
```
git clone https://github.com/KaisukaTran/findmy-fm
cd findmy-fm
pip install -r requirements-prod.txt
```

2. **Run API**:
```
uvicorn src.findmy.api.main:app --reload --host 0.0.0.0 --port 8000
```

3. **Access**:
- API: http://localhost:8000/docs
- Dashboard: http://localhost:8000/

## Docker Local

1. **Build & Run**:
```
docker-compose up --build
```

2. **Access**: http://localhost:8000/docs

## Production (Cloud/VM)

1. **Docker**:
```
docker pull your-registry/findmy-fm:latest
docker run -p 8000:8000 -v /path/to/data:/app/data -e LIVE_TRADING=false your-registry/findmy-fm:latest
```

2. **Kubernetes** (future):
- Helm chart planned v1.0

3. **Environment Vars**:
```
APP_SECRET_KEY=your-strong-secret
LIVE_TRADING=true
BROKER_API_KEY=your-binance-key
BROKER_API_SECRET=your-binance-secret
SOT_DATABASE_URL=postgresql://user:pass@host/db
```

## Live Trading Setup

1. Set `LIVE_TRADING=true`
2. Add Binance testnet keys first (`sandbox: True`)
3. Approve pending → live market order

**Warning**: Use testnet keys for production testing!
