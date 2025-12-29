# FINDMY – Configuration & Secrets Management

> **Security First**: This document explains how to safely handle secrets and configuration in FINDMY.

---

## Quick Start

1. **Copy the example file**:
   ```bash
   cp .env.example .env
   ```

2. **Fill in your secrets** in `.env` (local development only):
   ```bash
   APP_SECRET_KEY=your-strong-random-secret-here
   BROKER_API_KEY=your-api-key
   BROKER_API_SECRET=your-api-secret
   ```

3. **Never commit `.env`**: It's in `.gitignore` automatically.

4. **In production/cloud**: Set environment variables directly (no `.env` file needed).

---

## Configuration System

FINDMY uses **pydantic-settings** for robust, type-safe configuration management.

### Location

- **Configuration code**: [src/findmy/config.py](../src/findmy/config.py)
- **Example file**: [.env.example](.env.example) ✅ Committed to git
- **Local secrets**: `.env` ⛔ Ignored by git (local development only)
- **Production**: Environment variables set by container/cloud platform

### Settings Class

All settings are defined in `Settings` class in [src/findmy/config.py](../src/findmy/config.py):

```python
from findmy import settings

# Access settings anywhere in your code
print(settings.app_secret_key)
print(settings.broker_api_key)
print(settings.database_url)
```

---

## Environment Variables

### Required

| Variable | Purpose | Example |
|----------|---------|---------|
| `APP_SECRET_KEY` | JWT signing, session encryption | `your-strong-random-string` |

### Optional (Future Use)

| Variable | Purpose | Example |
|----------|---------|---------|
| `BROKER_API_KEY` | Live trading API key (v2.0+) | `sk_live_abc123...` |
| `BROKER_API_SECRET` | Live trading API secret (v2.0+) | `secret_xyz789...` |
| `BROKER_BASE_URL` | Broker API endpoint (v2.0+) | `https://api.example.com` |
| `DATABASE_URL` | Custom database URL | `postgresql://user:pass@host/db` |

---

## Local Development Setup

### Step 1: Create `.env` File

```bash
# In project root
cp .env.example .env
```

### Step 2: Edit `.env` with Your Values

```dotenv
# For local testing, you can use placeholder values
APP_SECRET_KEY=dev-secret-key-change-in-production

# Live trading credentials (optional for now)
BROKER_API_KEY=
BROKER_API_SECRET=
BROKER_BASE_URL=
```

### Step 3: Run the Application

The FastAPI app automatically loads `.env`:

```bash
uvicorn src.findmy.api.main:app --reload
```

Settings are loaded **once at startup** from:
1. Environment variables (highest priority)
2. `.env` file
3. Default values in code

---

## Production Deployment

**NEVER use `.env` files in production.**

Instead, set environment variables directly:

### Docker

```dockerfile
FROM python:3.12-slim

WORKDIR /app
COPY . .

# Install dependencies
RUN pip install -r requirements-prod.txt

# Environment variables set by docker run or docker-compose
# docker run -e APP_SECRET_KEY=$SECRET_KEY ...
CMD ["uvicorn", "src.findmy.api.main:app", "--host", "0.0.0.0"]
```

### Environment Variable in Docker Compose

```yaml
services:
  api:
    image: findmy:latest
    environment:
      APP_SECRET_KEY: ${APP_SECRET_KEY}  # From host environment
      BROKER_API_KEY: ${BROKER_API_KEY}
      DATABASE_URL: postgresql://user:pass@db:5432/findmy
```

### Kubernetes Secrets

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: findmy-secrets
type: Opaque
data:
  app-secret-key: YmFzZTY0LWVuY29kZWQtdmFsdWU=
  broker-api-key: YW5vdGhlci1iYXNlNjQtdmFsdWU=

---
apiVersion: v1
kind: Pod
metadata:
  name: findmy-api
spec:
  containers:
  - name: api
    image: findmy:latest
    env:
    - name: APP_SECRET_KEY
      valueFrom:
        secretKeyRef:
          name: findmy-secrets
          key: app-secret-key
    - name: BROKER_API_KEY
      valueFrom:
        secretKeyRef:
          name: findmy-secrets
          key: broker-api-key
```

### GitHub Codespaces

Environment variables can be set in Codespaces secrets:
1. Go to **Settings → Codespaces → Secrets**
2. Add `APP_SECRET_KEY`, `BROKER_API_KEY`, etc.
3. Variables are automatically available in your terminal

---

## Secret Rotation

### How to Rotate `APP_SECRET_KEY`

1. Generate a new secret:
   ```bash
   openssl rand -hex 32
   ```

2. Update in production:
   ```bash
   # Update your deployment configuration
   # Restart affected services
   ```

3. If using JWT tokens: Existing tokens signed with old key will **not validate** with new key
   - Plan accordingly (users may need to log in again)
   - Or use key versioning (advanced)

---

## Security Best Practices

### ✅ DO

- ✅ Use strong, random secrets (min 32 characters)
- ✅ Set unique `APP_SECRET_KEY` in production
- ✅ Use `SecretStr` fields (never logged)
- ✅ Rotate secrets regularly
- ✅ Use environment variables in production
- ✅ Use `.gitignore` to exclude `.env`
- ✅ Review `.env.example` before committing changes

### ❌ DON'T

- ❌ Commit `.env` file with real secrets
- ❌ Log `SecretStr` fields (pydantic prevents this)
- ❌ Share secrets in chat, tickets, or logs
- ❌ Use the same secret in multiple environments
- ❌ Leave `APP_SECRET_KEY` as development value in production
- ❌ Hardcode API keys in source code

---

## Troubleshooting

### "APP_SECRET_KEY is required"

The `APP_SECRET_KEY` variable is not set.

**Fix**: Add it to `.env` or set as environment variable:
```bash
export APP_SECRET_KEY="your-secret-key"
```

### Settings Not Loading from `.env`

1. Check `.env` is in project root (not in `src/`)
2. Ensure `.env` file has correct format: `KEY=value`
3. Restart the application
4. Check file permissions: `.env` should be readable

### Broker Credentials Not Working

1. Verify credentials are correct in `.env` or environment
2. Check broker API is accessible from your network
3. Check for IP whitelisting requirements
4. Verify API key has required permissions

---

## Related Documentation

- [API Reference](api.md) – REST endpoints
- [Architecture](architecture.md) – System design
- [Contributing](../CONTRIBUTING.md) – How to contribute code

---

## Questions?

For more information, see:
- [pydantic-settings documentation](https://docs.pydantic.dev/latest/concepts/pydantic_settings/)
- [12-factor app config](https://12factor.net/config)
- FINDMY [GitHub Issues](https://github.com/KaisukaTran/findmy-fm/issues)
