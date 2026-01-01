# Security Features (v0.8.0)

## JWT Authentication

**Enabled**: All sensitive endpoints require `Authorization: Bearer <token>`

**Endpoints**:
- `/api/auth/login` - POST username/password → tokens
- `/api/auth/refresh` - POST refresh_token → new access token
- `/api/auth/me` - GET current user info

**Token Usage**:
```
Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...
```

**Scopes**: `read`, `write`

**Expiration**: 60 minutes (access), long-lived refresh

## Rate Limiting (slowapi)

**Global**: 100 requests/minute/IP, 1000/day/IP

**Endpoint-specific**:
- Trading: 30/minute
- Data: 60/minute
- Login: 5/minute

**Key**: Remote IP address

## CORS

**Allowed Origins**:
- `http://localhost:3000`
- `http://localhost:8080`
- `https://yourdomain.com`

**Methods**: GET, POST, PUT, DELETE, OPTIONS

**Headers**: `*`

**Credentials**: True

## Security Headers

- `X-Content-Type-Options: nosniff`
- `X-Frame-Options: DENY`
- `X-XSS-Protection: 1; mode=block`
- `Strict-Transport-Security: max-age=31536000; includeSubDomains`
- `Content-Security-Policy: default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'`

## Trusted Hosts

`localhost`, `127.0.0.1`, `testserver`, `yourdomain.com`

## Input Validation

- File MIME type validation
- File size limits (10MB)
- Symbol validation
- Quantity/price validation
- Pydantic models

## Error Handling

- Consistent JSON errors with `ErrorResponse`
- No stack traces in production
- Rate limit exceeded handler
- Validation error handler

## Logging

- Request/response logging with duration
- Error logging with context
- Metrics integration (Prometheus)

## v0.8.0 Audit Compliance

✅ JWT protection on trading endpoints
✅ Rate limiting implemented
✅ CORS configured
✅ Security headers added
✅ DB session management
✅ Pagination on list endpoints
✅ Custom error handlers
✅ Logging middleware
