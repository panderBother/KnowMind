from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decode_access_claims
from app.db.session import get_db
from app.models.orm import User

security_bearer = HTTPBearer(auto_error=False)


async def get_current_user_id(
    cred: HTTPAuthorizationCredentials | None = Depends(security_bearer),
    session: AsyncSession = Depends(get_db),
) -> str:
    if cred is None or not cred.credentials:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "未登录或缺少 Authorization 头")
    claims = decode_access_claims(cred.credentials)
    if claims is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "令牌无效或已过期")
    uid = str(claims["sub"])
    user = await session.get(User, uid)
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "账号不存在或已停用")
    if int(claims.get("ver", 0)) != int(user.token_version or 0):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "令牌已撤销，请重新登录")
    return uid
