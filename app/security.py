from datetime import datetime, timedelta, timezone
import jwt
from fastapi import HTTPException, Header
from .config import settings

def make_token(subject: str, role: str):
    return jwt.encode({"sub":subject,"role":role,"exp":datetime.now(timezone.utc)+timedelta(hours=12)}, settings.jwt_secret, algorithm="HS256")

def require_role(allowed=None):
    async def dep(authorization: str|None=Header(default=None)):
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(401, "Login required")
        try:
            p=jwt.decode(authorization[7:], settings.jwt_secret, algorithms=["HS256"])
        except Exception:
            raise HTTPException(401, "Invalid token")
        role=p.get("role")
        order={"MODERATOR":1,"MANAGER":2,"SUPER_ADMIN":3,"OWNER":4}
        if allowed and order.get(role,0) < order.get(allowed,0):
            raise HTTPException(403, "Insufficient permission")
        return p
    return dep
