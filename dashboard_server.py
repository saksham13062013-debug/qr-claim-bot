import os, sqlite3, time
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

DB_PATH=os.getenv("DB_PATH","qr_claim_bot.db")
app=FastAPI(title="QR Claim Admin API")

def db():
    c=sqlite3.connect(DB_PATH)
    c.row_factory=sqlite3.Row
    return c

@app.get("/")
def dashboard():
    return FileResponse("dashboard/index.html")

@app.get("/api/admin/stats")
def stats():
    c=db()
    def one(sql):
        return c.execute(sql).fetchone()[0]
    data={
        "users":one("SELECT COUNT(*) FROM users"),
        "claims":one("SELECT COUNT(*) FROM claims"),
        "withdrawals":one("SELECT COUNT(*) FROM withdrawals"),
        "pending_claims":one("SELECT COUNT(*) FROM claims WHERE status='under_review'"),
        "pending_withdrawals":one("SELECT COUNT(*) FROM withdrawals WHERE status='pending'"),
        "processing":one("SELECT COUNT(*) FROM withdrawals WHERE status='processing'"),
        "rejected":one("SELECT COUNT(*) FROM withdrawals WHERE status='rejected'"),
        "paid_amount":one("SELECT COALESCE(SUM(amount),0) FROM withdrawals WHERE status='paid'")
    }
    c.close(); return data

@app.get("/api/admin/withdrawals")
def withdrawals():
    c=db()
    rows=c.execute("SELECT id,user_id,amount,method,status FROM withdrawals ORDER BY id DESC LIMIT 100").fetchall()
    c.close()
    return [dict(x) for x in rows]

class StatusChange(BaseModel):
    status:str

@app.post("/api/admin/withdrawals/{wid}")
def change_status(wid:int, body:StatusChange):
    if body.status not in {"processing","paid","rejected"}:
        raise HTTPException(400,"Invalid status")
    c=db(); row=c.execute("SELECT status FROM withdrawals WHERE id=?",(wid,)).fetchone()
    if not row: c.close(); raise HTTPException(404,"Withdrawal not found")
    old=row["status"]
    valid=(old=="pending" and body.status in {"processing","rejected"}) or (old=="processing" and body.status in {"paid","rejected"})
    if not valid:
        c.close(); raise HTTPException(409,f"Invalid transition {old}->{body.status}")
    c.execute("UPDATE withdrawals SET status=?,reviewed_at=? WHERE id=?",(body.status,time.strftime("%Y-%m-%d %H:%M:%S"),wid))
    c.commit(); c.close()
    # The bot's own notification/refund logic remains the source of truth for user messaging.
    return {"ok":True,"status":body.status}
