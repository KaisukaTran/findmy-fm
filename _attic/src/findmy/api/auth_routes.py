"""
Authentication Routes (v0.7.0)

JWT-based authentication endpoints with rate limiting protection.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from datetime import timedelta
from services.auth.service import (
    authenticate_user,
    create_access_token,
    create_refresh_token,
    verify_token,
    get_user,
    User,
)
from src.findmy.api.schemas import LoginRequest, TokenResponse, UserResponse

router = APIRouter(prefix="/api/auth", tags=["Authentication"])


@router.post("/login", response_model=TokenResponse)
async def login(request: LoginRequest):
    """
    User login endpoint - returns JWT access and refresh tokens.
    
    v0.7.0: Rate limited to prevent brute force attacks.
    """
    user = authenticate_user(request.username, request.password)
    if not user:
        # Don't reveal whether username exists (security best practice)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "bearer"},
        )
    
    # Create tokens
    access_token_expires = timedelta(minutes=60)
    access_token = create_access_token(
        data={"sub": user.username, "scopes": ["read", "write"]},
        expires_delta=access_token_expires
    )
    refresh_token = create_refresh_token(data={"sub": user.username})
    
    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=int(access_token_expires.total_seconds()),
    )


@router.post("/refresh", response_model=TokenResponse)
async def refresh_token(request: TokenResponse):
    """
    Refresh an expired access token using a refresh token.
    """
    token_data = verify_token(request.refresh_token)
    if not token_data:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token",
        )
    
    # Create new access token
    access_token_expires = timedelta(minutes=60)
    access_token = create_access_token(
        data={"sub": token_data.sub, "scopes": ["read", "write"]},
        expires_delta=access_token_expires
    )
    
    return TokenResponse(
        access_token=access_token,
        refresh_token=request.refresh_token,
        expires_in=int(access_token_expires.total_seconds()),
    )


@router.get("/me", response_model=UserResponse)
async def get_current_user(token: str = None):
    """
    Get current authenticated user information.
    """
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication token",
        )
    
    token_data = verify_token(token)
    if not token_data:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )
    
    user = get_user(token_data.sub)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )
    
    return UserResponse(**user.dict())
