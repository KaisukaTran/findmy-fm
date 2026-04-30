"""
Authentication Service (v0.7.0)

Provides JWT-based authentication and authorization for API endpoints.
"""

from datetime import datetime, timedelta
from typing import Optional
from jose import JWTError, jwt

from src.findmy.config import settings as _cfg
from services.auth.password import hash_password, verify_password

# Derive JWT secret from app settings – fail loudly if weak or default.
_raw_secret = _cfg.app_secret_key.get_secret_value()
if len(_raw_secret) < 32:
    raise RuntimeError(
        "APP_SECRET_KEY must be at least 32 characters. "
        "Set a strong random value before starting the server."
    )
SECRET_KEY = _raw_secret

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60
REFRESH_TOKEN_EXPIRE_DAYS = 30


class Token:
    """JWT Token response."""
    def __init__(self, access_token: str, refresh_token: str, token_type: str = "bearer", expires_in: int = 0):
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.token_type = token_type
        self.expires_in = expires_in


class TokenData:
    """Token payload data."""
    def __init__(self, sub: str, scopes: list = None, iat: datetime = None, exp: datetime = None):
        self.sub = sub
        self.scopes = scopes or []
        self.iat = iat
        self.exp = exp


class User:
    """User model."""
    def __init__(self, username: str, email: Optional[str] = None, full_name: Optional[str] = None, disabled: bool = False):
        self.username = username
        self.email = email
        self.full_name = full_name
        self.disabled = disabled
    
    def dict(self):
        return {
            "username": self.username,
            "email": self.email,
            "full_name": self.full_name,
            "disabled": self.disabled,
        }


class UserInDB(User):
    """User with bcrypt-hashed password."""
    def __init__(self, username: str, password_hash: str, email: Optional[str] = None, full_name: Optional[str] = None, disabled: bool = False):
        super().__init__(username, email, full_name, disabled)
        self.password_hash = password_hash


def create_access_token(
    data: dict,
    expires_delta: Optional[timedelta] = None
) -> str:
    """Create a JWT access token."""
    to_encode = data.copy()
    
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    
    to_encode.update({"exp": expire, "iat": datetime.utcnow()})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt


def create_refresh_token(data: dict) -> str:
    """Create a JWT refresh token (longer expiry)."""
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    to_encode.update({"exp": expire, "iat": datetime.utcnow(), "type": "refresh"})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt


def verify_token(token: str) -> Optional[TokenData]:
    """Verify and decode a JWT token."""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            return None
        
        token_data = TokenData(
            sub=username,
            scopes=payload.get("scopes", []),
            iat=datetime.fromtimestamp(payload.get("iat", 0)),
            exp=datetime.fromtimestamp(payload.get("exp", 0)),
        )
        return token_data
    except JWTError:
        return None


def _db_user_to_model(db_user) -> UserInDB:
    return UserInDB(
        username=db_user.username,
        password_hash=db_user.password_hash,
        disabled=not db_user.is_active,
    )


_DEMO_RAW = {
    "trader1": ("password123", "trader1@findmy.io", "Trader One"),
    "trader2": ("password456", "trader2@findmy.io", "Trader Two"),
}
_DEMO_USERS: dict[str, UserInDB] = {}  # populated lazily on first auth attempt


def _get_demo_users() -> dict[str, UserInDB]:
    """Build demo user dict on first use — avoids 300ms bcrypt at import time."""
    global _DEMO_USERS
    if not _DEMO_USERS:
        _DEMO_USERS = {
            username: UserInDB(
                username=username,
                password_hash=hash_password(pw),
                email=email,
                full_name=name,
            )
            for username, (pw, email, name) in _DEMO_RAW.items()
        }
    return _DEMO_USERS


def _has_db_users() -> bool:
    try:
        from services.auth.user_repository import list_users
        return bool(list_users())
    except Exception:
        return False


def authenticate_user(username: str, password: str) -> Optional[UserInDB]:
    """Authenticate against DB first; demo store fallback when DB is empty."""
    if _has_db_users():
        try:
            from services.auth.user_repository import authenticate as db_auth
            db_user = db_auth(username, password)
            return _db_user_to_model(db_user) if db_user else None
        except Exception:
            return None
    user = _get_demo_users().get(username)
    if user and verify_password(password, user.password_hash):
        return user
    return None


def get_user(username: str) -> Optional[UserInDB]:
    """Get user from DB, fallback to demo store."""
    try:
        from services.auth.user_repository import get_by_username
        db_user = get_by_username(username)
        if db_user:
            return _db_user_to_model(db_user)
    except Exception:
        pass
    return _get_demo_users().get(username)

