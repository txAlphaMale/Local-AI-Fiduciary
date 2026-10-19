# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""
emergency_api.py -- HTTP layer for emergency.py and the Emergency Kit (TASK-002). register(app).

app.py's global gate lets viewers do EVERY GET and blocks viewer writes except an allowlist, so:
  * every GET here enforces its own role check (emergency.status_for is role-scoped);
  * the ONLY viewer-reachable writes are /api/emergency/request and /api/emergency/cancel (added
    to _VIEWER_ALLOWED_WRITE_PATHS) and each re-checks "is the designee" itself;
  * every other write is admin-only via an explicit check here (never just the gate).
"""
import os

from fastapi import Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask

import audit
import auth
import emergency
import kit
import notifications


def _user(request):
    uid = request.session.get("uid")
    return auth.get_user_by_id(uid) if uid else None


def _err(e):
    return JSONResponse({"error": str(e)}, status_code=getattr(e, "status", 400))


def _unauth():
    return JSONResponse({"error": "authentication required"}, status_code=401)


def _admin_only(request):
    u = _user(request)
    if not u:
        return None, _unauth()
    if not auth.is_admin(u):
        return None, JSONResponse({"error": "admin only"}, status_code=403)
    return u, None


def register(app, binder_fn):
    """binder_fn() -> dict of contacts/executor/will location/account map for the kit export."""

    @app.get("/api/emergency/status")
    def em_status(request: Request):
        u = _user(request)
        if not u:
            return _unauth()
        return emergency.status_for(u)          # role-scoped: admin=all, designee=own, others={is_designee:false}

    @app.post("/api/emergency/config")
    async def em_config(request: Request):
        admin, bad = _admin_only(request)
        if bad:
            return bad
        body = await request.json()
        if not isinstance(body, dict):
            return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
        try:
            return {"ok": True, **emergency.update_config(admin, body)}
        except emergency.EmergencyError as e:
            return _err(e)

    @app.post("/api/emergency/test-notification")
    def em_test(request: Request):
        admin, bad = _admin_only(request)
        if bad:
            return bad
        res = notifications.send_test(admin["id"], True)
        ok = res.get("push_sent", 0) > 0 or res.get("email_sent")
        if ok:
            emergency.mark_test_ok()
        return {"ok": bool(ok), "result": res,
                "note": None if ok else "No push device or email accepted the test — subscribe a device in "
                                         "SETTINGS → NOTIFICATIONS or configure SMTP first."}

    @app.post("/api/emergency/request")
    def em_request(request: Request):
        u = _user(request)
        if not u:
            return _unauth()
        try:
            req = emergency.request_access(u)
        except emergency.EmergencyError as e:
            audit.emit("authz.denied", outcome="denied", request=request, actor=u["id"],
                       actor_name=u.get("username"), resource="/api/emergency/request",
                       detail={"reason": str(e)})
            return _err(e)
        return {"ok": True, "status": emergency.status_for(u)}

    @app.post("/api/emergency/cancel")
    def em_cancel(request: Request):
        u = _user(request)
        if not u:
            return _unauth()
        try:
            emergency.cancel(u)
        except emergency.EmergencyError as e:
            return _err(e)
        return {"ok": True, "status": emergency.status_for(u)}

    def _admin_action(fn):
        def h(request: Request):
            admin, bad = _admin_only(request)
            if bad:
                return bad
            try:
                fn(admin)
            except emergency.EmergencyError as e:
                return _err(e)
            return {"ok": True, "status": emergency.status_for(admin)}
        return h

    app.post("/api/emergency/deny")(_admin_action(emergency.deny))
    app.post("/api/emergency/approve")(_admin_action(emergency.approve))
    app.post("/api/emergency/revoke")(_admin_action(emergency.revoke))

    # ---- Emergency Kit (admin only) ----
    @app.get("/api/vault/kit/status")
    def kit_status(request: Request):
        admin, bad = _admin_only(request)
        if bad:
            return bad
        return kit.status()

    @app.post("/api/vault/kit")
    async def kit_generate(request: Request):
        admin, bad = _admin_only(request)
        if bad:
            return bad
        try:
            body = await request.json()
        except Exception:
            body = {}
        pw = (body or {}).get("passphrase") or None
        try:
            res = kit.build(binder_fn(), pw)
        except kit.KitError as e:
            return _err(e)
        audit.emit("vault.kit_generated", request=request, actor=admin["id"], actor_name=admin.get("username"),
                   resource="/api/vault/kit", detail={"items": res["item_count"], "size": res["size"],
                                                      "custom_passphrase": bool(pw)})   # never the passphrase
        return JSONResponse({"ok": True, "passphrase": res["passphrase"], "download_token": res["download_token"],
                             "item_count": res["item_count"], "size": res["size"], "name": res["name"],
                             "note": "This passphrase is shown ONCE and is not stored anywhere. Write it down "
                                     "and keep it apart from the kit file."},
                            headers={"Cache-Control": "no-store"})

    @app.get("/api/vault/kit/{token}")
    def kit_download(token: str, request: Request):
        admin, bad = _admin_only(request)
        if bad:
            return bad
        got = kit.claim(token)
        if not got:
            return JSONResponse({"error": "download link expired or already used"}, status_code=404)
        path, name = got
        audit.emit("vault.kit_downloaded", request=request, actor=admin["id"], actor_name=admin.get("username"),
                   resource="/api/vault/kit", detail={"name": name})
        return FileResponse(path, media_type="application/zip", filename=name,
                            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
                            background=BackgroundTask(lambda: os.path.exists(path) and os.remove(path)))
