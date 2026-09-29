from __future__ import annotations

import hmac
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import Config, verify_password
from .db import Database
from .models import ConnectionSettings, DeploymentSpec, Login, RedeployRequest
from .service import DeploymentError, DeploymentService

STATIC = Path(__file__).parent / "static"
COOKIE = "gdeploy_session"


def create_app(config: Config | None = None, start_worker=True, service_factory=DeploymentService):
    @asynccontextmanager
    async def lifespan(app):
        app.state.config = config or Config.from_env()
        app.state.db = Database(app.state.config.data_dir, app.state.config.secret_key)
        app.state.service = service_factory(app.state.db, app.state.config)
        app.state.login_lock = threading.Lock()
        if start_worker:
            app.state.service.start()
        yield
        if start_worker:
            app.state.service.stop()

    app = FastAPI(title="GDeploy", version="0.1.0", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    def authenticated(request: Request):
        session = request.app.state.db.session(request.cookies.get(COOKIE))
        if not session:
            raise HTTPException(401, "Sign in to continue.")
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            token = request.headers.get("X-CSRF-Token", "")
            if not hmac.compare_digest(token.encode(), session["csrf"].encode()):
                raise HTTPException(403, "Session verification failed. Refresh and try again.")
        return session

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"
        if getattr(request.app.state, "config", None) and request.app.state.config.cookie_secure:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request, exc):
        # FastAPI's default response includes submitted values (including passwords).
        return JSONResponse(
            status_code=422,
            content={
                "detail": [
                    {"loc": list(error["loc"]), "msg": error["msg"], "type": error["type"]} for error in exc.errors()
                ]
            },
        )

    @app.exception_handler(DeploymentError)
    async def deployment_error(request, exc):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.get("/api/health")
    def health(request: Request):
        service = request.app.state.service
        if service.worker_error or (service.thread is not None and not service.thread.is_alive()):
            raise HTTPException(503, "Deployment worker is unavailable.")
        return {"status": "ok"}

    @app.get("/api/session")
    def session(request: Request):
        current = request.app.state.db.session(request.cookies.get(COOKIE))
        return {
            "authenticated": bool(current),
            **({"username": current["username"], "csrf_token": current["csrf"]} if current else {}),
        }

    @app.post("/api/login")
    def login(payload: Login, request: Request, response: Response):
        # Reject cross-origin login to prevent login CSRF; no cross-origin API is exposed.
        origin = request.headers.get("Origin")
        if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
            raise HTTPException(403, "Sign in from the GDeploy page.")
        cfg, db = request.app.state.config, request.app.state.db
        ip = request.client.host if request.client else "unknown"
        with request.app.state.login_lock:
            if not db.login_allowed(ip):
                raise HTTPException(429, "Too many failed sign-in attempts. Try again in 15 minutes.")
            valid_password = verify_password(payload.password, cfg.admin_password_hash)
            if not hmac.compare_digest(payload.username.encode(), cfg.admin_username.encode()) or not valid_password:
                db.failed_login(ip)
                raise HTTPException(401, "Incorrect username or password.")
            if request.cookies.get(COOKIE):
                db.delete_session(request.cookies[COOKIE])
            token, csrf = db.new_session(cfg.admin_username, cfg.session_hours)
            db.audit("Administrator signed in")
        response.set_cookie(
            COOKIE,
            token,
            max_age=cfg.session_hours * 3600,
            httponly=True,
            secure=cfg.cookie_secure,
            samesite="strict",
            path="/",
        )
        return {"authenticated": True, "username": cfg.admin_username, "csrf_token": csrf}

    @app.post("/api/logout")
    def logout(request: Request, response: Response, current=Depends(authenticated)):
        request.app.state.db.delete_session(request.cookies[COOKIE])
        response.delete_cookie(
            COOKIE, path="/", secure=request.app.state.config.cookie_secure, httponly=True, samesite="strict"
        )
        return {"ok": True}

    @app.get("/api/settings")
    def settings(request: Request, current=Depends(authenticated)):
        saved = request.app.state.db.settings() or {}
        cfg = request.app.state.config
        return {
            "host": saved.get("host", ""),
            "username": saved.get("username", ""),
            "verify_tls": saved.get("verify_tls", True),
            "configured": bool(saved),
            "iso_configured": os.access(cfg.ubuntu_iso, os.R_OK)
            and cfg.ubuntu_iso.is_file()
            and bool(cfg.ubuntu_sha256),
            "splunk_configured": os.access(cfg.splunk_package, os.R_OK)
            and cfg.splunk_package.is_file()
            and bool(cfg.splunk_sha256),
        }

    @app.put("/api/settings")
    def save_settings(payload: ConnectionSettings, request: Request, current=Depends(authenticated)):
        db = request.app.state.db
        value = payload.model_dump()
        previous = db.settings()
        if not value["password"]:
            if not previous or previous["host"] != value["host"] or previous["username"] != value["username"]:
                raise HTTPException(400, "Supply the ESXi password when setting or changing the connection.")
            value["password"] = previous["password"]
        db.set_settings(value)
        return {"ok": True}

    @app.get("/api/inventory")
    def inventory(request: Request, current=Depends(authenticated)):
        return request.app.state.service.inventory()

    @app.get("/api/deployments")
    def deployments(request: Request, current=Depends(authenticated)):
        return request.app.state.db.list()

    @app.get("/api/deployments/{deployment_id}")
    def detail(deployment_id: UUID, request: Request, current=Depends(authenticated)):
        item = request.app.state.db.get(str(deployment_id))
        if not item:
            raise HTTPException(404, "Deployment not found.")
        item["events"] = request.app.state.db.events(str(deployment_id))
        return item

    @app.post("/api/preflight")
    def preflight(payload: DeploymentSpec, request: Request, current=Depends(authenticated)):
        return request.app.state.service.preflight(payload.model_dump())

    @app.post("/api/deployments", status_code=201)
    def create_deployment(payload: DeploymentSpec, request: Request, current=Depends(authenticated)):
        service = request.app.state.service
        with service.action_lock:
            return service.enqueue(payload.model_dump())

    @app.post("/api/deployments/{deployment_id}/credentials")
    def credentials(deployment_id: UUID, request: Request, current=Depends(authenticated)):
        if not request.app.state.db.get(str(deployment_id)):
            raise HTTPException(404, "Deployment not found.")
        return request.app.state.service.credentials(str(deployment_id))

    @app.post("/api/deployments/{deployment_id}/redeploy", status_code=201)
    def redeploy(deployment_id: UUID, payload: RedeployRequest, request: Request, current=Depends(authenticated)):
        service = request.app.state.service
        with service.action_lock:
            return service.redeploy(str(deployment_id), payload.confirm_name)

    app.mount("/static", StaticFiles(directory=STATIC, check_dir=False), name="static")

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    return app


app = create_app()
