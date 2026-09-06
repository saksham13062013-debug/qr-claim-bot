from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pathlib import Path
app=FastAPI(title="QR Claim Admin")
@app.get("/",response_class=HTMLResponse)
def home():
    return Path("dashboard/index.html").read_text(encoding="utf-8")
