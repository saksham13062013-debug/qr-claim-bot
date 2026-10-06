from datetime import datetime, timezone
from decimal import Decimal
import secrets, string
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, func, update, delete
from sqlalchemy.ext.asyncio import AsyncSession
from .db import SessionLocal
from .models import *
from .config import settings
from .security import make_token, require_role

router=APIRouter()

async def db():
    async with SessionLocal() as s:
        yield s

class LoginIn(BaseModel):
    username:str
    password:str
class QRIn(BaseModel):
    rewardUsd:float=Field(gt=0)
    expiresAt:datetime|None=None
    code:str|None=None
class ClaimIn(BaseModel):
    telegramId:int
    username:str|None=None
class ReviewIn(BaseModel):
    approve:bool
    amountUsd:float|None=None
    reason:str|None=None
class WithdrawalIn(BaseModel):
    telegramId:int
    amountUsd:float=Field(gt=0)
    method:str
    paymentDetails:dict[str,str]
class WithdrawalReview(BaseModel):
    status:str
    note:str|None=None
class ForceJoinIn(BaseModel):
    channelId:int
    username:str|None=None
    title:str|None=None
    required:bool=True
class PromoteIn(BaseModel):
    telegramId:int
    role:str

def money(v:float)->int: return int(round(v*100))
def ref(prefix:str)->str: return f"{prefix}-{secrets.token_hex(8)}"

async def audit(s, actor_id, action, target_type, target_id=None, metadata=None):
    s.add(AuditLog(actor_id=actor_id,action=action,target_type=target_type,target_id=str(target_id) if target_id else None,metadata_json=metadata or {}))

@router.get("/", response_class=HTMLResponse)
async def home():
    return open("app/templates/admin.html", encoding="utf-8").read()

@router.post("/auth/login")
async def login(x:LoginIn):
    if x.username!=settings.admin_username or x.password!=settings.admin_password:
        raise HTTPException(401,"Invalid credentials")
    return {"token":make_token(str(settings.owner_telegram_id),"OWNER"),"role":"OWNER"}

@router.get("/dashboard")
async def dashboard(s:AsyncSession=Depends(db), _:dict=Depends(require_role())):
    vals={}
    vals["users"]=await s.scalar(select(func.count(User.id)))
    vals["claims"]=await s.scalar(select(func.count(QRClaim.id)))
    vals["pendingRewards"]=await s.scalar(select(func.count(QRClaim.id)).where(QRClaim.status=="PENDING"))
    vals["pendingWithdrawals"]=await s.scalar(select(func.count(Withdrawal.id)).where(Withdrawal.status=="PENDING"))
    vals["activeQR"]=await s.scalar(select(func.count(QRCode.id)).where(QRCode.status.in_(["AVAILABLE","POSTED"])))
    return vals

@router.get("/users")
async def users(s:AsyncSession=Depends(db), _:dict=Depends(require_role())):
    rows=(await s.scalars(select(User).order_by(User.created_at.desc()).limit(100))).all()
    return [{"id":u.id,"telegramId":u.telegram_id,"username":u.username,"status":u.status,"balance":u.balance_cents/100,"createdAt":u.created_at} for u in rows]

@router.post("/qr")
async def create_qr(x:QRIn,s:AsyncSession=Depends(db),p:dict=Depends(require_role("MANAGER"))):
    code=x.code or "QR-"+''.join(secrets.choice(string.ascii_uppercase+string.digits) for _ in range(8))
    # owner is the authenticated Telegram id; ensure user exists
    u=await s.scalar(select(User).where(User.telegram_id==settings.owner_telegram_id))
    if not u:
        u=User(telegram_id=settings.owner_telegram_id,username=settings.admin_username); s.add(u); await s.flush()
    q=QRCode(code=code,reward_cents=money(x.rewardUsd),expires_at=x.expiresAt,created_by_id=u.id,status="DRAFT")
    s.add(q); await s.flush(); await audit(s,u.id,"QR_CREATE","QR",code,{"rewardUsd":x.rewardUsd}); await s.commit()
    return {"id":q.id,"code":q.code,"rewardUsd":q.reward_cents/100,"status":q.status,"expiresAt":q.expires_at}

@router.post("/qr/{code}/post")
async def post_qr(code:str,s:AsyncSession=Depends(db),p:dict=Depends(require_role("MANAGER"))):
    q=await s.scalar(select(QRCode).where(QRCode.code==code))
    if not q: raise HTTPException(404,"not found")
    q.status="AVAILABLE"; q.posted_at=datetime.now(timezone.utc); await audit(s,int(p["sub"]),"QR_POST","QR",code); await s.commit()
    return {"ok":True,"code":code,"status":q.status}

@router.post("/qr/{code}/reclaim")
async def reclaim(code:str,s:AsyncSession=Depends(db),p:dict=Depends(require_role("SUPER_ADMIN"))):
    q=await s.scalar(select(QRCode).where(QRCode.code==code).with_for_update())
    if not q: raise HTTPException(404,"not found")
    c=await s.scalar(select(QRClaim).where(QRClaim.qr_id==q.id).where(QRClaim.status!="REVOKED"))
    if c: c.status="REVOKED"; c.reviewed_at=datetime.now(timezone.utc); c.reviewed_by_id=int(p["sub"]); c.reason="QR reclaimed by admin"
    q.status="RECLAIMED"; await audit(s,int(p["sub"]),"QR_RECLAIM","QR",code); await s.commit()
    return {"ok":True}

@router.post("/qr/{code}/claim")
async def claim(code:str,x:ClaimIn,s:AsyncSession=Depends(db)):
    async with s.begin():
        u=await s.scalar(select(User).where(User.telegram_id==x.telegramId))
        if not u:
            u=User(telegram_id=x.telegramId,username=x.username); s.add(u); await s.flush()
        q=await s.scalar(select(QRCode).where(QRCode.code==code).with_for_update())
        if not q or q.status not in ("AVAILABLE","POSTED","RECLAIMED"): raise HTTPException(400,"QR_UNAVAILABLE")
        if q.expires_at and q.expires_at < datetime.now(timezone.utc): raise HTTPException(400,"QR_EXPIRED")
        q.status="PENDING"
        c=QRClaim(qr_id=q.id,user_id=u.id,reward_cents=q.reward_cents,status="PENDING"); s.add(c)
        s.add(Notification(user_id=u.id,type="CLAIM_PENDING",message=f"Your QR claim {q.code} is pending approval."))
    return {"ok":True,"status":"PENDING","claimId":c.id}

@router.post("/claims/{claim_id}/review")
async def review_claim(claim_id:int,x:ReviewIn,s:AsyncSession=Depends(db),p:dict=Depends(require_role("MANAGER"))):
    async with s.begin():
        c=await s.scalar(select(QRClaim).where(QRClaim.id==claim_id).with_for_update())
        if not c or c.status!="PENDING": raise HTTPException(400,"CLAIM_NOT_PENDING")
        q=await s.get(QRCode,c.qr_id); u=await s.get(User,c.user_id)
        if x.approve:
            amount=c.reward_cents if x.amountUsd is None else money(x.amountUsd)
            before=u.balance_cents; u.balance_cents+=amount
            s.add(WalletTransaction(user_id=u.id,type="QR_REWARD",amount_cents=amount,balance_before=before,balance_after=u.balance_cents,reference_id=ref("REWARD"),metadata_json={"qr":q.code,"claimId":c.id}))
            c.status="APPROVED"; c.reward_cents=amount; c.reviewed_at=datetime.now(timezone.utc); c.reviewed_by_id=int(p["sub"]); q.status="CLAIMED"
            s.add(Notification(user_id=u.id,type="CLAIM_SUCCESS",message=f"🎉 Reward approved: ${(amount/100):.2f} added to your wallet."))
            out="approved"
        else:
            c.status="REJECTED"; c.reason=x.reason or "Rejected by admin"; c.reviewed_at=datetime.now(timezone.utc); c.reviewed_by_id=int(p["sub"]); q.status="AVAILABLE"
            s.add(Notification(user_id=u.id,type="CLAIM_FAILED",message=f"❌ QR claim rejected. {x.reason or ''}")); out="rejected"
    return {"ok":True,"status":out}

@router.post("/withdrawals")
async def withdrawal(x:WithdrawalIn,s:AsyncSession=Depends(db)):
    amount=money(x.amountUsd)
    if amount<money(settings.min_withdrawal_usd) or amount>money(settings.max_withdrawal_usd): raise HTTPException(400,"amount_out_of_range")
    async with s.begin():
        u=await s.scalar(select(User).where(User.telegram_id==x.telegramId).with_for_update())
        if not u or u.balance_cents<amount: raise HTTPException(400,"INSUFFICIENT_BALANCE")
        u.balance_cents-=amount
        w=Withdrawal(user_id=u.id,amount_cents=amount,method=x.method,payment_details=x.paymentDetails); s.add(w)
    return {"ok":True,"id":w.id,"status":w.status}

@router.post("/withdrawals/{wid}/review")
async def withdrawal_review(wid:int,x:WithdrawalReview,s:AsyncSession=Depends(db),p:dict=Depends(require_role("SUPER_ADMIN"))):
    if x.status not in ("PROCESSING","PAID","REJECTED"): raise HTTPException(400,"invalid")
    async with s.begin():
        w=await s.scalar(select(Withdrawal).where(Withdrawal.id==wid).with_for_update())
        if not w or w.status in ("PAID","REJECTED"): raise HTTPException(400,"INVALID_WITHDRAWAL")
        if x.status=="REJECTED":
            u=await s.get(User,w.user_id); before=u.balance_cents; u.balance_cents+=w.amount_cents
            s.add(WalletTransaction(user_id=u.id,type="WITHDRAWAL_REVERSAL",amount_cents=w.amount_cents,balance_before=before,balance_after=u.balance_cents,reference_id=ref("REVERSAL")))
        w.status=x.status; w.reviewed_by_id=int(p["sub"]); w.review_note=x.note
    return {"ok":True,"status":x.status}

@router.get("/logs")
async def logs(s:AsyncSession=Depends(db), _:dict=Depends(require_role())):
    rows=(await s.scalars(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(100))).all()
    return [{"id":r.id,"actorId":r.actor_id,"action":r.action,"targetType":r.target_type,"targetId":r.target_id,"metadata":r.metadata_json,"createdAt":r.created_at} for r in rows]

@router.get("/qr")
async def list_qr(s:AsyncSession=Depends(db), _:dict=Depends(require_role())):
    rows=(await s.scalars(select(QRCode).order_by(QRCode.created_at.desc()).limit(100))).all()
    return [{"id":q.id,"code":q.code,"rewardUsd":q.reward_cents/100,"status":q.status,"expiresAt":q.expires_at,"createdAt":q.created_at,"postedAt":q.posted_at} for q in rows]

@router.get("/claims")
async def list_claims(s:AsyncSession=Depends(db), _:dict=Depends(require_role())):
    rows=(await s.scalars(select(QRClaim).order_by(QRClaim.claimed_at.desc()).limit(100))).all()
    return [{"id":c.id,"qrId":c.qr_id,"userId":c.user_id,"status":c.status,"rewardUsd":c.reward_cents/100,"claimedAt":c.claimed_at,"reason":c.reason} for c in rows]

@router.get("/withdrawals")
async def list_withdrawals(s:AsyncSession=Depends(db), _:dict=Depends(require_role())):
    rows=(await s.scalars(select(Withdrawal).order_by(Withdrawal.created_at.desc()).limit(100))).all()
    return [{"id":w.id,"userId":w.user_id,"amountUsd":w.amount_cents/100,"method":w.method,"status":w.status,"createdAt":w.created_at,"reviewNote":w.review_note} for w in rows]

@router.get("/force-join")
async def force_join_list(s:AsyncSession=Depends(db)):
    rows=(await s.scalars(select(ForceJoinChannel).where(ForceJoinChannel.status=="ACTIVE").order_by(ForceJoinChannel.created_at))).all()
    return [{"id":x.id,"channelId":x.channel_id,"username":x.username,"title":x.title,"required":x.required,"status":x.status} for x in rows]

@router.post("/force-join")
async def force_join_add(x:ForceJoinIn,s:AsyncSession=Depends(db),p:dict=Depends(require_role("SUPER_ADMIN"))):
    c=ForceJoinChannel(channel_id=x.channelId,username=x.username,title=x.title,required=x.required); s.add(c); await s.commit(); return {"id":c.id,"channelId":c.channel_id}

@router.get("/health")
async def health(): return {"ok":True}
