"""
WSPBDBot Dashboard - Admin viewer for whispers
Run: uvicorn dashboard:app --host 0.0.0.0 --port 8000
Or: python dashboard.py
"""
import os
import secrets
from typing import Optional

from fastapi import FastAPI, Request, Form, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from data import config
# Use unified Group-manager DB (manager.db) which now includes Messages + users/groups
try:
    from database import db as unified_db
    db = unified_db
except Exception as e:
    from utils.db_api.sqlite import Database
    db = Database(path_to_db="data/main.db")
    print(f"Fallback DB: {e}")

app = FastAPI(title="WSPBDBot Dashboard - Unified with Group-manager")
# Session secret - use env or random
SESSION_SECRET = os.getenv("DASHBOARD_SECRET") or os.getenv("ADMIN_PASSWORD") or secrets.token_hex(32)
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, max_age=86400)

templates = Jinja2Templates(directory="templates")

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD") or "admin123"  # change in .env
# Allow ADMINS list also via Telegram ID bypass? For web we use password.

def is_logged_in(request: Request) -> bool:
    return request.session.get("admin_auth") is True

def require_auth(request: Request):
    if not is_logged_in(request):
        raise HTTPException(status_code=401)

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    if is_logged_in(request):
        return RedirectResponse(url="/admin", status_code=302)
    return RedirectResponse(url="/admin/login", status_code=302)

@app.get("/admin/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if is_logged_in(request):
        return RedirectResponse(url="/admin", status_code=302)
    return templates.TemplateResponse("admin_login.html", {"request": request, "error": None})

@app.post("/admin/login", response_class=HTMLResponse)
async def login_submit(request: Request, password: str = Form(...)):
    if password == ADMIN_PASSWORD:
        request.session["admin_auth"] = True
        return RedirectResponse(url="/admin", status_code=302)
    return templates.TemplateResponse("admin_login.html", {"request": request, "error": "Wrong password"})

@app.get("/admin/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/admin/login", status_code=302)

@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(request: Request, page: int = Query(1, ge=1), search: Optional[str] = Query(None), limit: int = Query(20, ge=5, le=100), tab: str = Query("whispers")):
    if not is_logged_in(request):
        return RedirectResponse(url="/admin/login", status_code=302)
    offset = (page - 1) * limit
    try:
        db.create_table_messages()
    except:
        pass
    rows = db.get_whispers(limit=limit, offset=offset, search=search)
    total_row = db.count_whispers(search=search)
    total = total_row[0] if total_row else 0
    total_pages = (total + limit - 1) // limit if total else 1
    # rows: tuple with 10 columns
    whispers = []
    for r in rows or []:
        try:
            # handle both sqlite3.Row and tuple
            if hasattr(r, "keys"):
                whispers.append({
                    "message_id": r["message_id"],
                    "message_text": r["message_text"],
                    "sender_name": r["name"],
                    "sender_id": r["user_id"],
                    "sender_username": r["tg_username"] or "",
                    "targets": r["targets"] or "",
                    "secret": r["secret"] or "",
                    "fake": r["fake"] or "",
                    "chat_id": r["chat_id"] or "",
                    "created_at": r["created_at"] or "",
                })
            else:
                whispers.append({
                    "message_id": r[0],
                    "message_text": r[1],
                    "sender_name": r[2],
                    "sender_id": r[3],
                    "sender_username": r[4] or "",
                    "targets": r[5] or "",
                    "secret": r[6] or "",
                    "fake": r[7] or "",
                    "chat_id": r[8] or "",
                    "created_at": r[9] or "",
                })
        except IndexError:
            whispers.append({
                "message_id": r[0],
                "message_text": r[1],
                "sender_name": r[2] if len(r) > 2 else "",
                "sender_id": r[3] if len(r) > 3 else "",
                "sender_username": r[4] if len(r) > 4 else "",
                "targets": "",
                "secret": "",
                "fake": "",
                "chat_id": "",
                "created_at": "",
            })
    # Also fetch Group-manager data for dashboard stats
    try:
        users = db.get_all_users() if hasattr(db, "get_all_users") else {}
        groups = db.get_all_groups() if hasattr(db, "get_all_groups") else {}
        stats = db.get_all_stats() if hasattr(db, "get_all_stats") else {}
        logs = db.get_recent_logs(20) if hasattr(db, "get_recent_logs") else []
    except:
        users, groups, stats, logs = {}, {}, {}, []
    return templates.TemplateResponse("admin.html", {
        "request": request,
        "whispers": whispers,
        "page": page,
        "total_pages": total_pages,
        "total": total,
        "search": search or "",
        "limit": limit,
        "admins": config.ADMINS,
        "tab": tab,
        "users": users,
        "groups": groups,
        "stats": stats,
        "logs": logs,
    })

@app.get("/admin/api/whispers")
async def api_whispers(request: Request, page: int = Query(1, ge=1), search: Optional[str] = Query(None), limit: int = Query(20, ge=5, le=100)):
    if not is_logged_in(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    offset = (page - 1) * limit
    rows = db.get_whispers(limit=limit, offset=offset, search=search)
    total = (db.count_whispers(search=search) or [0])[0]
    data = []
    for r in rows or []:
        try:
            data.append({
                "message_id": r[0],
                "message_text": r[1],
                "sender_name": r[2],
                "sender_id": r[3],
                "sender_username": r[4] or "",
                "targets": r[5] or "",
                "secret": r[6] or "",
                "fake": r[7] or "",
                "created_at": str(r[9] or ""),
            })
        except:
            data.append({"message_id": r[0], "message_text": r[1]})
    return {"whispers": data, "total": total, "page": page, "limit": limit}

@app.post("/admin/api/delete/{msg_id}")
async def api_delete(request: Request, msg_id: str):
    if not is_logged_in(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    db.delete_whisper(msg_id)
    return {"ok": True}

@app.get("/health")
async def health():
    return {"ok": True, "bot": "WSPBDBot"}

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("DASHBOARD_PORT", "8000"))
    uvicorn.run("dashboard:app", host="0.0.0.0", port=port, reload=True)
