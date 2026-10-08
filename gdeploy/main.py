from __future__ import annotations

import hmac
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, StrictBool

from . import __version__
from .certificate_trust import CertificateTrust, CertificateTrustError, certificate_endpoint
from .config import Config, hash_password, verify_password
from .db import Database, DeploymentStopError, DeploymentVisibilityError, MediaStateError
from .deployment_defaults import DeploymentDefaults, DeploymentDefaultsError
from .fleetmanager import FleetManager, FleetManagerError
from .media import MediaError, MediaManager
from .models import (
    AccountSetup,
    CertificateApproval,
    CertificateHost,
    ConnectionSettings,
    DeploymentDefaultsSettings,
    DeploymentSpec,
    ESXiMediaSelection,
    FleetManagerSettings,
    FleetManagerVersionLookup,
    Login,
    MediaSelection,
    RedeployRequest,
    SSHKeySettings,
    SplunkPackageSelection,
)
from .packages import PackageError, SplunkPackageManager
from .service import DeploymentError, DeploymentService
from .ssh_keys import SSHKeyError, ssh_public_key_details
from .vmware import VMwareError

STATIC = Path(__file__).parent / "static"
COOKIE = "gdeploy_session"


class DeploymentVisibility(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hidden: StrictBool


def create_app(config: Config | None = None, start_worker=True, service_factory=DeploymentService):
    @asynccontextmanager
    async def lifespan(app):
        app.state.config = config or Config.from_env()
        app.state.db = Database(app.state.config.data_dir, app.state.config.secret_key)
        app.state.db.ensure_administrator(
            app.state.config.admin_username, app.state.config.admin_password_hash
        )
        app.state.service = service_factory(app.state.db, app.state.config)
        app.state.deployment_defaults = DeploymentDefaults(app.state.db, app.state.service)
        app.state.certificates = CertificateTrust(app.state.db)
        app.state.media = MediaManager(app.state.db, app.state.config)
        app.state.media.recover_uploads()
        app.state.packages = SplunkPackageManager(app.state.db, app.state.config)
        app.state.packages.recover_uploads()
        app.state.fleetmanager = FleetManager(app.state.db, app.state.config)
        app.state.fleetmanager.recover_uploads()
        app.state.login_lock = threading.Lock()
        if start_worker:
            app.state.service.start()
        yield
        if start_worker:
            app.state.service.stop()

    app = FastAPI(
        title="GDeploy", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )

    def session_details(request: Request, allow_pending: bool = False):
        # Read the session and current account together with respect to login/setup.
        # A restricted session must never inherit access while another request finishes setup.
        with request.app.state.login_lock:
            db = request.app.state.db
            current = db.session(request.cookies.get(COOKIE))
            administrator = db.administrator()
            if not current or current["username"] != administrator["username"]:
                raise HTTPException(401, "Sign in to continue.")
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                token = request.headers.get("X-CSRF-Token", "")
                if not hmac.compare_digest(token.encode(), current["csrf"].encode()):
                    raise HTTPException(403, "Session verification failed. Refresh and try again.")
            if administrator["must_change_credentials"] and not allow_pending:
                raise HTTPException(403, detail={
                    "code": "credentials_change_required",
                    "message": "Choose new administrator credentials before continuing.",
                })
            return current

    def authenticated(request: Request):
        return session_details(request)

    def signed_in(request: Request):
        return session_details(request, allow_pending=True)

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
        elif request.url.path == "/" or request.url.path.startswith("/static/"):
            # Revalidate on reload, including conditional asset responses. The
            # release query in index.html also bypasses previously cached URLs.
            response.headers["Cache-Control"] = "no-cache"
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

    @app.exception_handler(CertificateTrustError)
    async def certificate_error(request, exc):
        return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})

    @app.exception_handler(MediaError)
    async def media_error(request, exc):
        return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})

    @app.exception_handler(MediaStateError)
    async def media_conflict(request, exc):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(PackageError)
    async def package_error(request, exc):
        return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})

    @app.exception_handler(SSHKeyError)
    async def ssh_key_error(request, exc):
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(FleetManagerError)
    async def fleetmanager_error(request, exc):
        return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})

    @app.exception_handler(DeploymentDefaultsError)
    async def deployment_defaults_error(request, exc):
        return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})

    @app.get("/api/health")
    def health(request: Request):
        service = request.app.state.service
        if service.worker_error or (service.thread is not None and not service.thread.is_alive()):
            raise HTTPException(503, "Deployment worker is unavailable.")
        return {"status": "ok"}

    @app.get("/api/session")
    def session(request: Request):
        with request.app.state.login_lock:
            db = request.app.state.db
            current = db.session(request.cookies.get(COOKIE))
            administrator = db.administrator()
            if not current or current["username"] != administrator["username"]:
                return {"authenticated": False}
            return {
                "authenticated": True,
                "username": current["username"],
                "csrf_token": current["csrf"],
                "must_change_credentials": administrator["must_change_credentials"],
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
            administrator = db.administrator()
            valid_password = verify_password(payload.password, administrator["password_hash"])
            if not hmac.compare_digest(payload.username.encode(), administrator["username"].encode()) or not valid_password:
                db.failed_login(ip)
                raise HTTPException(401, "Incorrect username or password.")
            if request.cookies.get(COOKIE):
                db.delete_session(request.cookies[COOKIE])
            token, csrf = db.new_session(administrator["username"], cfg.session_hours)
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
        return {
            "authenticated": True, "username": administrator["username"], "csrf_token": csrf,
            "must_change_credentials": administrator["must_change_credentials"],
        }

    @app.post("/api/account/setup")
    def complete_setup(payload: AccountSetup, request: Request, response: Response, current=Depends(signed_in)):
        with request.app.state.login_lock:
            completed = request.app.state.db.complete_initial_setup(
                request.cookies.get(COOKIE), payload.username, hash_password(payload.password)
            )
            if not completed:
                raise HTTPException(409, "Initial account setup is already complete or your session expired. Sign in again.")
        response.delete_cookie(
            COOKIE, path="/", secure=request.app.state.config.cookie_secure, httponly=True, samesite="strict"
        )
        return {"ok": True}

    @app.post("/api/logout")
    def logout(request: Request, response: Response, current=Depends(signed_in)):
        request.app.state.db.delete_session(request.cookies[COOKIE])
        response.delete_cookie(
            COOKIE, path="/", secure=request.app.state.config.cookie_secure, httponly=True, samesite="strict"
        )
        return {"ok": True}

    @app.get("/api/settings")
    def settings(request: Request, current=Depends(authenticated)):
        saved = request.app.state.db.settings() or {}
        fleetmanager = request.app.state.fleetmanager.catalog(include_packages=False)
        return {
            "host": saved.get("host", ""),
            "username": saved.get("username", ""),
            "verify_tls": True,
            "configured": bool(saved),
            "certificate_trust": request.app.state.certificates.saved(saved["host"]) if saved else None,
            "iso_configured": request.app.state.media.catalog()["ready"],
            "ssh_key_count": len(request.app.state.db.ssh_public_keys()),
            "splunk_configured": request.app.state.packages.catalog()["ready"],
            "fleetmanager_configured": fleetmanager["ready"],
            "fleetmanager_mode": fleetmanager["mode"],
            "deployment_defaults": request.app.state.deployment_defaults.catalog(),
        }

    @app.get("/api/settings/deployment-defaults")
    def deployment_defaults(request: Request, current=Depends(authenticated)):
        return request.app.state.deployment_defaults.catalog()

    @app.put("/api/settings/deployment-defaults")
    def save_deployment_defaults(payload: DeploymentDefaultsSettings, request: Request, current=Depends(authenticated)):
        return request.app.state.deployment_defaults.save(payload.host, payload.default_network)

    @app.delete("/api/settings/deployment-defaults")
    def clear_deployment_defaults(request: Request, current=Depends(authenticated)):
        return request.app.state.deployment_defaults.clear()

    @app.get("/api/settings/ssh-keys")
    def ssh_keys(request: Request, current=Depends(authenticated)):
        keys = ssh_public_key_details(request.app.state.db.ssh_public_keys())
        return {"keys": keys, "count": len(keys)}

    @app.get("/api/settings/splunk-package")
    def splunk_package_settings(request: Request, current=Depends(authenticated)):
        return request.app.state.packages.catalog()

    @app.get("/api/settings/fleetmanager")
    def fleetmanager_settings(request: Request, current=Depends(authenticated)):
        return request.app.state.fleetmanager.catalog()

    @app.put("/api/settings/fleetmanager")
    def save_fleetmanager_settings(payload: FleetManagerSettings, request: Request, current=Depends(authenticated)):
        return request.app.state.fleetmanager.save(payload)

    @app.delete("/api/settings/fleetmanager")
    def clear_fleetmanager_settings(request: Request, current=Depends(authenticated)):
        return request.app.state.fleetmanager.clear()

    @app.post("/api/settings/fleetmanager/versions")
    def fleetmanager_versions(payload: FleetManagerVersionLookup, request: Request, current=Depends(authenticated)):
        return request.app.state.fleetmanager.repository_versions(payload.repository_token)

    @app.post("/api/settings/fleetmanager/packages/upload")
    async def upload_fleetmanager_package(
        request: Request, filename: str, sha256: str | None = None, current=Depends(authenticated),
    ):
        return await request.app.state.fleetmanager.upload(request, filename, sha256)

    @app.delete("/api/settings/fleetmanager/packages/{package_id}")
    def delete_fleetmanager_package(package_id: str, request: Request, current=Depends(authenticated)):
        return request.app.state.fleetmanager.delete(package_id)

    @app.put("/api/settings/splunk-package")
    def select_splunk_package(payload: SplunkPackageSelection, request: Request, current=Depends(authenticated)):
        return request.app.state.packages.select(payload.package_id, payload.sha256, sha512=payload.sha512)

    @app.delete("/api/settings/splunk-package")
    def clear_splunk_package(request: Request, current=Depends(authenticated)):
        return request.app.state.packages.clear_selection()

    @app.post("/api/settings/splunk-package/upload")
    async def upload_splunk_package(
        request: Request, filename: str, sha256: str | None = None, sha512: str | None = None,
        current=Depends(authenticated),
    ):
        return await request.app.state.packages.upload(request, filename, sha256, sha512=sha512)

    @app.delete("/api/settings/splunk-package/{package_id}")
    def delete_splunk_package(package_id: str, request: Request, current=Depends(authenticated)):
        return request.app.state.packages.delete(package_id)

    @app.put("/api/settings/ssh-keys")
    def save_ssh_keys(payload: SSHKeySettings, request: Request, current=Depends(authenticated)):
        saved = request.app.state.db.set_ssh_public_keys(payload.public_keys)
        keys = ssh_public_key_details(saved)
        return {"keys": keys, "count": len(keys)}

    @app.put("/api/settings")
    def save_settings(payload: ConnectionSettings, request: Request, current=Depends(authenticated)):
        db = request.app.state.db
        value = payload.model_dump()
        with request.app.state.service.action_lock:
            previous = db.settings()
            if not value["password"]:
                if not previous or previous["host"] != value["host"] or previous["username"] != value["username"]:
                    raise HTTPException(400, "Supply the ESXi password when setting or changing the connection.")
                value["password"] = previous["password"]
            db.set_settings(value)
        return {"ok": True}

    @app.get("/api/settings/media")
    def media_settings(request: Request, current=Depends(authenticated)):
        return request.app.state.media.catalog()

    @app.delete("/api/settings/media")
    def clear_media_selection(request: Request, current=Depends(authenticated)):
        return request.app.state.media.clear_selection()

    @app.get("/api/settings/storage")
    def storage_settings(request: Request, current=Depends(authenticated)):
        return request.app.state.media.storage()

    @app.delete("/api/settings/media/{media_id}")
    def delete_media(media_id: str, request: Request, current=Depends(authenticated)):
        return request.app.state.media.delete(media_id)

    @app.put("/api/settings/media")
    def select_media(payload: MediaSelection, request: Request, current=Depends(authenticated)):
        return request.app.state.media.select(payload.media_id, payload.sha256)

    @app.post("/api/settings/media/upload")
    async def upload_media(request: Request, filename: str, sha256: str, current=Depends(authenticated)):
        return await request.app.state.media.upload(request, filename, sha256)

    @app.get("/api/settings/media/esxi")
    def browse_esxi_media(
        request: Request, datastore: str | None = Query(default=None, max_length=128),
        folder: str = Query(default="", max_length=2048), current=Depends(authenticated),
    ):
        saved = request.app.state.db.settings()
        if not saved:
            raise MediaError("Save the ESXi connection in Setup before browsing its installation media.")
        try:
            with request.app.state.service.client(saved) as esxi:
                datastores = esxi.inventory()["datastores"]
                chosen = datastore if datastore is not None else next((item["name"] for item in datastores), "")
                if not chosen:
                    return {"host": saved["host"], "datastores": datastores, "datastore": "", "folder": "",
                            "parent": None, "folders": [], "files": []}
                return {**esxi.browse_iso_media(chosen, folder), "host": saved["host"], "datastores": datastores}
        except VMwareError as exc:
            raise MediaError(str(exc)) from None

    @app.post("/api/settings/media/esxi")
    def import_esxi_media(payload: ESXiMediaSelection, request: Request, current=Depends(authenticated)):
        saved = request.app.state.db.settings()
        if not saved:
            raise MediaError("Save the ESXi connection in Setup before selecting its installation media.")
        if certificate_endpoint(saved["host"])[2] != certificate_endpoint(payload.host)[2]:
            raise MediaError("The saved ESXi host changed. Browse its datastores again before selecting an ISO.", 409)
        try:
            with request.app.state.service.client(saved) as esxi:
                return request.app.state.media.import_esxi(
                    esxi, saved["host"], payload.datastore, payload.path, payload.sha256
                )
        except VMwareError as exc:
            raise MediaError(str(exc)) from None

    @app.get("/api/inventory")
    def inventory(request: Request, current=Depends(authenticated)):
        return request.app.state.service.inventory()

    @app.post("/api/settings/certificate/inspect")
    def inspect_certificate(payload: CertificateHost, request: Request, current=Depends(authenticated)):
        return request.app.state.certificates.inspect(payload.host)

    @app.post("/api/settings/certificate/trust")
    def trust_certificate(payload: CertificateApproval, request: Request, current=Depends(authenticated)):
        return request.app.state.certificates.trust(payload.host, payload.fingerprint_sha256)

    @app.delete("/api/settings/certificate")
    def remove_certificate(payload: CertificateHost, request: Request, current=Depends(authenticated)):
        request.app.state.certificates.remove(payload.host)
        return {"ok": True}

    @app.get("/api/deployments")
    def deployments(request: Request, include_hidden: bool = False, current=Depends(authenticated)):
        return request.app.state.db.list(include_hidden=include_hidden)

    @app.get("/api/deployments/{deployment_id}")
    def detail(deployment_id: UUID, request: Request, current=Depends(authenticated)):
        item = request.app.state.db.get(str(deployment_id))
        if not item:
            raise HTTPException(404, "Deployment not found.")
        item["events"] = request.app.state.db.events(str(deployment_id))
        return item

    @app.patch("/api/deployments/{deployment_id}/visibility")
    def deployment_visibility(
        deployment_id: UUID, payload: DeploymentVisibility, request: Request, current=Depends(authenticated)
    ):
        service = request.app.state.service
        db = request.app.state.db
        with service.action_lock:
            try:
                item = db.set_visibility(str(deployment_id), payload.hidden)
            except DeploymentVisibilityError as exc:
                raise HTTPException(409, str(exc)) from None
            if item is None:
                raise HTTPException(404, "Deployment not found.")
            item["events"] = db.events(str(deployment_id))
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

    @app.post("/api/deployments/{deployment_id}/stop")
    def stop_deployment(deployment_id: UUID, request: Request, current=Depends(authenticated)):
        try:
            item = request.app.state.service.request_stop(str(deployment_id))
        except DeploymentStopError as error:
            raise HTTPException(409, str(error)) from None
        if item is None:
            raise HTTPException(404, "Deployment not found.")
        item["events"] = request.app.state.db.events(str(deployment_id))
        return item

    @app.post("/api/deployments/{deployment_id}/redeploy", status_code=201)
    def redeploy(deployment_id: UUID, payload: RedeployRequest, request: Request, current=Depends(authenticated)):
        service = request.app.state.service
        with service.action_lock:
            return service.redeploy(str(deployment_id), payload.confirm_name)

    app.mount("/static", StaticFiles(directory=STATIC, check_dir=False), name="static")

    @app.get("/")
    def index():
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(html.replace("__GDEPLOY_VERSION__", __version__))

    return app


app = create_app()
