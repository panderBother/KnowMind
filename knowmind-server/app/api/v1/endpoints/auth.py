from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user_id
from app.core.security import decode_refresh_token
from app.db.session import get_db
from app.schemas.auth import (
    AuthOkResponse,
    ChangePasswordRequest,
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    UserPublic,
)
from app.services import auth_service
from app.services.rate_limit_service import enforce_auth_rate

router = APIRouter()


@router.post("/register", response_model=AuthOkResponse)
async def register(
    body: RegisterRequest,
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    await enforce_auth_rate(request)
    try:
        user, tok = await auth_service.register_user(session, body.email, body.password)
    except auth_service.AuthError as e:
        raise HTTPException(e.status_code, detail={"code": e.code, "message": e.message}) from e
    return AuthOkResponse(
        user=user,
        access_token=tok.access_token,
        refresh_token=tok.refresh_token,
        token_type=tok.token_type,
        expires_in=tok.expires_in,
        refresh_expires_in=tok.refresh_expires_in,
    )


@router.post("/login", response_model=AuthOkResponse)
async def login(
    body: LoginRequest,
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    await enforce_auth_rate(request)
    try:
        user, tok = await auth_service.login_user(session, body.email, body.password)
    except auth_service.AuthError as e:
        raise HTTPException(e.status_code, detail={"code": e.code, "message": e.message}) from e
    return AuthOkResponse(
        user=user,
        access_token=tok.access_token,
        refresh_token=tok.refresh_token,
        token_type=tok.token_type,
        expires_in=tok.expires_in,
        refresh_expires_in=tok.refresh_expires_in,
    )


@router.get("/me", response_model=UserPublic)
async def me(
    session: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    user = await auth_service.get_user_by_id(session, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "用户不存在")
    return UserPublic.model_validate(user)


@router.post("/refresh", response_model=AuthOkResponse)
async def refresh(
    body: RefreshRequest,
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    await enforce_auth_rate(request)
    user_id = decode_refresh_token(body.refresh_token)
    if not user_id:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail={"code": "INVALID_REFRESH", "message": "刷新令牌无效"})
    try:
        tok = await auth_service.refresh_tokens(session, body.refresh_token)
        user = await auth_service.get_user_by_id(session, user_id)
    except auth_service.AuthError as e:
        raise HTTPException(e.status_code, detail={"code": e.code, "message": e.message}) from e
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户不存在")
    return AuthOkResponse(
        user=UserPublic.model_validate(user),
        access_token=tok.access_token,
        refresh_token=tok.refresh_token,
        token_type=tok.token_type,
        expires_in=tok.expires_in,
        refresh_expires_in=tok.refresh_expires_in,
    )


@router.post("/change-password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    body: ChangePasswordRequest,
    session: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    try:
        await auth_service.change_password(
            session,
            user_id,
            current_password=body.current_password,
            new_password=body.new_password,
        )
    except auth_service.AuthError as e:
        raise HTTPException(e.status_code, detail={"code": e.code, "message": e.message}) from e
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    session: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """撤销该账号当前签发的全部 access/refresh token。"""
    await auth_service.revoke_all_tokens(session, user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
