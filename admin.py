import os, sqlite3, time, secrets
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session, flash, send_file
from dotenv import load_dotenv
import qrcode

load_dotenv()
app=Flask(__name__)
app.secret_key=os.getenv("FLASK_SECRET",secrets.token_hex(32))
DB_PATH=os.getenv("DB_PATH","qr_work.db")
ADMIN_USER=os.getenv("ADMIN_USERNAME","admin")
ADMIN_PASS=os.getenv("ADMIN_PASSWORD","change-this-password")
CURRENCY=os.getenv("CURRENCY","POINTS")
TASK_TTL=int(os.getenv("TASK_TTL_MINUTES","30"))*60

def db():
    c=sqlite3.connect(DB_PATH); c.row_factory=sqlite3.Row; return c
def now(): return int(time.time())

STYLE_PRESETS = {
  "modern": {
    "task_header": "🆕 <b>New task #{task_id} · {title}</b>",
    "task_reward": "💰 Your reward: <b>{reward:.2f} {currency}</b>",
    "task_expiry": "⏳ Expires: <b>{expires}</b>",
    "task_warning": "⚠️ <b>Claim before opening the payment link.</b>",
    "task_footer": "The system assigns the earliest-expiring task currently open to you. Task numbers in older messages do not select a particular task."
  },
  "minimal": {
    "task_header": "🆕 <b>Task #{task_id}</b> · {title}",
    "task_reward": "💰 Reward: <b>{reward:.2f} {currency}</b>",
    "task_expiry": "⏳ Expires: <b>{expires}</b>",
    "task_warning": "⚠️ Claim before opening the payment link.",
    "task_footer": "Earliest-expiring open task is assigned automatically."
  },
  "premium": {
    "task_header": "╭━━━ ✦ <b>NEW TASK</b> ✦ ━━━╮\n┃ #{task_id} · {title}",
    "task_reward": "┃ 💰 Reward: <b>{reward:.2f} {currency}</b>",
    "task_expiry": "┃ ⏳ Expires: <b>{expires}</b>",
    "task_warning": "┃ ⚠️ Claim before opening the payment link.",
    "task_footer": "╰━━━━━━━━━━━━━━━━━━━━╯\nThe earliest-expiring open task is assigned automatically."
  }
}

def settings_db(c):
    c.execute("""CREATE TABLE IF NOT EXISTS settings(
      key TEXT PRIMARY KEY, value TEXT NOT NULL)""")
    c.commit()

def get_style(c):
    settings_db(c)
    name = c.execute("SELECT value FROM settings WHERE key='style'").fetchone()
    name = name["value"] if name else "modern"
    st = STYLE_PRESETS.get(name, STYLE_PRESETS["modern"]).copy()
    for key in ("task_header","task_reward","task_expiry","task_warning","task_footer"):
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        if row and row["value"]:
            st[key] = row["value"]
    return name, st

def auth(f):
    @wraps(f)
    def w(*a,**k):
        if not session.get("admin"): return redirect(url_for("login"))
        return f(*a,**k)
    return w

@app.route("/login",methods=["GET","POST"])
def login():
    if request.method=="POST":
        if secrets.compare_digest(request.form.get("username",""),ADMIN_USER) and secrets.compare_digest(request.form.get("password",""),ADMIN_PASS):
            session["admin"]=True; return redirect(url_for("dashboard"))
        flash("Invalid credentials")
    return render_template("login.html")

@app.get("/health")
def health():
    return {"status": "ok"}


@app.route("/style", methods=["GET","POST"])
@auth
def style():
    c=db(); settings_db(c)
    if request.method=="POST":
        preset=request.form.get("preset","modern")
        if preset not in STYLE_PRESETS: preset="modern"
        c.execute("""INSERT INTO settings(key,value) VALUES('style',?)
                     ON CONFLICT(key) DO UPDATE SET value=excluded.value""",(preset,))
        for key in ("task_header","task_reward","task_expiry","task_warning","task_footer"):
            c.execute("""INSERT INTO settings(key,value) VALUES(?,?)
                         ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                      (key, request.form.get(key,"").strip()))
        c.commit()
        flash("Message style saved. New bot messages will use this style.")
        return redirect(url_for("style"))
    name, st=get_style(c)
    return render_template("style.html", style_name=name, style=st, currency=CURRENCY)

@app.post("/style/preview")
@auth
def style_preview():
    values = {
      "task_id": 643, "title": "UPI 扫码任务",
      "reward": 0.7, "currency": CURRENCY, "expires": "10:05:50 UTC"
    }
    fields = {k: request.form.get(k,"") for k in
              ("task_header","task_reward","task_expiry","task_warning","task_footer")}
    preview = "\\n".join(fields[k].format(**values) for k in
                         ("task_header","task_reward","task_expiry","task_warning","task_footer"))
    return render_template("preview.html", preview=preview)

@app.get("/logout")
def logout():
    session.clear(); return redirect(url_for("login"))

@app.get("/")
@auth
def dashboard():
    c=db()
    stats={
      "users":c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"],
      "online":c.execute("SELECT COUNT(*) n FROM users WHERE active=1").fetchone()["n"],
      "open":c.execute("SELECT COUNT(*) n FROM tasks WHERE status='open'").fetchone()["n"],
      "claimed":c.execute("SELECT COUNT(*) n FROM tasks WHERE status='claimed'").fetchone()["n"],
      "review":c.execute("SELECT COUNT(*) n FROM tasks WHERE status='pending_review'").fetchone()["n"],
      "completed":c.execute("SELECT COUNT(*) n FROM tasks WHERE status='completed'").fetchone()["n"],
    }
    recent=c.execute("""SELECT t.*,u.username FROM tasks t LEFT JOIN users u ON u.id=t.claimed_by
                        ORDER BY t.id DESC LIMIT 12""").fetchall()
    return render_template("dashboard.html",stats=stats,recent=recent,currency=CURRENCY)

@app.route("/tasks",methods=["GET","POST"])
@auth
def tasks():
    c=db()
    if request.method=="POST":
        title=request.form.get("title","").strip()
        payload=request.form.get("payload","").strip()
        try: reward=float(request.form.get("reward","0.70"))
        except: reward=0.70
        if not title or not payload: flash("Title and QR payload are required")
        else:
            exp=now()+TASK_TTL
            cur=c.execute("""INSERT INTO tasks(title,qr_payload,reward,status,expires_at,created_at)
                             VALUES(?,?,?,'open',?,?)""",(title,payload,reward,exp,now()))
            c.commit(); flash(f"Task #{cur.lastrowid} created")
            return redirect(url_for("tasks"))
    rows=c.execute("""SELECT t.*,u.username FROM tasks t LEFT JOIN users u ON u.id=t.claimed_by
                      ORDER BY t.id DESC LIMIT 100""").fetchall()
    return render_template("tasks.html",tasks=rows,currency=CURRENCY)

@app.post("/tasks/<int:tid>/cancel")
@auth
def cancel_task(tid):
    c=db(); c.execute("UPDATE tasks SET status='cancelled' WHERE id=? AND status IN ('open','claimed')",(tid,)); c.commit()
    flash("Task cancelled"); return redirect(url_for("tasks"))

@app.get("/tasks/<int:tid>/qr")
@auth
def task_qr(tid):
    c=db(); t=c.execute("SELECT * FROM tasks WHERE id=?",(tid,)).fetchone()
    if not t: return "Not found",404
    img=qrcode.make(t["qr_payload"]); path=f"/tmp/task_{tid}.png"; img.save(path)
    return send_file(path,mimetype="image/png")

@app.get("/users")
@auth
def users():
    c=db(); rows=c.execute("SELECT * FROM users ORDER BY created_at DESC LIMIT 200").fetchall()
    return render_template("users.html",users=rows,currency=CURRENCY)

@app.get("/reviews")
@auth
def reviews():
    c=db(); rows=c.execute("""SELECT t.*,u.username,u.first_name FROM tasks t
      LEFT JOIN users u ON u.id=t.claimed_by WHERE t.status='pending_review' ORDER BY t.verified_at ASC""").fetchall()
    return render_template("reviews.html",tasks=rows,currency=CURRENCY)

@app.post("/reviews/<int:tid>/approve")
@auth
def approve(tid):
    c=db(); t=c.execute("SELECT * FROM tasks WHERE id=? AND status='pending_review'",(tid,)).fetchone()
    if t and t["claimed_by"]:
        c.execute("UPDATE tasks SET status='completed' WHERE id=?",(tid,))
        c.execute("UPDATE users SET balance=balance+? WHERE id=?",(t["reward"],t["claimed_by"]))
        c.execute("INSERT INTO task_history(task_id,user_id,action,created_at) VALUES(?,?,?,?)",(tid,t["claimed_by"],"approved",now()))
        c.commit(); flash(f"Task #{tid} approved and reward credited")
    return redirect(url_for("reviews"))

@app.post("/reviews/<int:tid>/reject")
@auth
def reject(tid):
    c=db(); t=c.execute("SELECT * FROM tasks WHERE id=? AND status='pending_review'",(tid,)).fetchone()
    if t:
        c.execute("UPDATE tasks SET status='rejected' WHERE id=?",(tid,))
        if t["claimed_by"]: c.execute("INSERT INTO task_history(task_id,user_id,action,created_at) VALUES(?,?,?,?)",(tid,t["claimed_by"],"rejected",now()))
        c.commit(); flash(f"Task #{tid} rejected")
    return redirect(url_for("reviews"))

if __name__=="__main__":
    host=os.getenv("ADMIN_HOST","127.0.0.1"); port=int(os.getenv("PORT", os.getenv("ADMIN_PORT","8080")))
    app.run(host=host,port=port,debug=False)
