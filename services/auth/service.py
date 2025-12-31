"""
Authentication Service (v0.7.0)

Provides JWT-based authentication and authorization for API endpoints.
"""

from datetime import datetime, timedelta
from typing import Optional
from jose import JWTError, jwt
import os

# Configuration
SECRET_KEY = os.getenv("SECRET_KEY", "your-secret-key-change-in-production")
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
    """User with password."""
    def __init__(self, username: str, password: str, email: Optional[str] = None, full_name: Optional[str] = None, disabled: bool = False):
        super().__init__(username, email, full_name, disabled)
        self.password = password


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


# Demo user database (replace with real database in production)
# Using plain text for demo; replace with hashed passwords in production
DEMO_USERS = {
    "trader1": UserInDB(
        username="trader1",
        password="password123",  # v0.7.0: For demo only; use hashed passwords in production
        email="trader1@findmy.io",
        full_name="Trader One",
        disabled=False,
    ),
    "trader2": UserInDB(
        username="trader2",
        password="password456",  # v0.7.0: For demo only
        email="trader2@findmy.io",
        full_name="Trader Two",
        disabled=False,
    ),
}


def authenticate_user(username: str, password: str) -> Optional[UserInDB]:
    """Authenticate a user (demo implementation)."""
    if username not in DEMO_USERS:
        return None
    
    user = DEMO_USERS[username]
    # For demo: simple password comparison (use bcrypt in production)
    if user.password != password:
        return None
    
    return user


def get_user(username: str) -> Optional[UserInDB]:
    """Get a user by username."""
    if username in DEMO_USERS:
        return DEMO_USERS[username]
    return None

