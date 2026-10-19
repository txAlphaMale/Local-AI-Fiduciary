"""
vault_api.py -- HTTP layer for vault.py (TASK-001 Phase 1). register(app) adds /api/vault/*.

app.py's global gate already denies viewer WRITES (POST/PUT/PATCH/DELETE not in the allowlist) but
lets viewers make EVERY GET, so each GET handler here enforces visibility itself via
vault.can_read(). An item the caller may not read answers 404 (not 403) so its existence isn't
revealed, and the denial is audited. Writes additionally require admin (defense in depth).
"""
from urllib.parse import quote, unquote

from fastapi import Request
from fastapi.responses import JSONResponse, Response

import audit
import auth
import vault


def _user(request):
    uid = request.session.get("uid")
    return auth.get_user_by_id(uid) if uid else None


def _unauth():
    return JSONResponse({"error": "authentication required"}, status_code=401)


def _err(e):
    return JSONResponse({"error": str(e)}, status_code=getattr(e, "status", 400))


def _emergency_granted(user):
    """True when `user` is the designee of a granted, unexpired emergency VIEW grant (re-evaluated
    against SQLite on every call -- deny/revoke/expiry take effect on the next request)."""
    import emergency
    return emergency.emergency_granted(user)


def _actor(user):
    return {"actor": user["id"], "actor_name": user.get("username")} if user else {}


def register(app):
    @app.get("/api/vault/folders")
    def vault_folders(request: Request):
        user = _user(request)
        if not user:
            return _unauth()
        folders = vault.list_folders()
        readable = vault.list_items(user, emergency_granted=_emergency_granted(user))
        counts = {}
        for it in readable:
            counts[it["folder"]] = counts.get(it["folder"], 0) + 1
        for f in folders:
            f["count"] = counts.get(f["name"], 0)
        return {"folders": folders, "is_admin": auth.is_admin(user),
                "limits": {"max_file_mb": vault.VAULT_MAX_FILE_MB,
                           "max_total_gb": vault.VAULT_MAX_TOTAL_GB,
                           "used_bytes": vault.total_bytes() if auth.is_admin(user) else None}}

    @app.get("/api/vault/items")
    def vault_items(request: Request, folder: str = ""):
        user = _user(request)
        if not user:
            return _unauth()
        return {"items": vault.list_items(user, folder or None, _emergency_granted(user))}

    def _readable_row(request, item_id, action):
        """(user, row) if the caller may read the item, else None after auditing the denial."""
        user = _user(request)
        if not user:
            return None
        row = vault.get_item_row(item_id)
        if row and vault.can_read(user, row, _emergency_granted(user)):
            return user, row
        if row and not row["deleted"]:
            audit.emit("authz.denied", outcome="denied", request=request, resource=f"/api/vault/{action}",
                       detail={"reason": "vault_visibility", "item": item_id}, **_actor(user))
        return None

    @app.get("/api/vault/items/{item_id}")
    def vault_item(item_id: str, request: Request):
        got = _readable_row(request, item_id, "meta")
        if not got:
            return JSONResponse({"error": "not found"}, status_code=404)
        return vault.public_item(got[1])

    @app.get("/api/vault/items/{item_id}/download")
    def vault_download(item_id: str, request: Request, inline: int = 0):
        got = _readable_row(request, item_id, "download")
        if not got:
            return JSONResponse({"error": "not found"}, status_code=404)
        user, row = got
        try:
            data = vault.read_plaintext(row)
        except vault.VaultError as e:
            return _err(e)
        item = vault.public_item(row)
        # emergency-visibility reads get their own resource prefix so detections.py can count a
        # designee's bulk downloads from plaintext audit columns without decrypting event detail.
        res = (f"/api/vault/emergency-items/{item_id}" if row["visibility"] == "emergency"
               else f"/api/vault/items/{item_id}")
        audit.emit("vault.download", request=request, resource=res,
                   detail={"item": item_id, "inline": bool(inline), "visibility": row["visibility"]},
                   **_actor(user))
        # Inline only for PDF/PNG/JPEG; everything else (text, DOCX, HEIC ...) is attachment-only.
        as_inline = bool(inline) and row["mime"] in vault.INLINE_MIMES
        fname = (item["filename"] or "document").replace('"', "").replace("\r", "").replace("\n", "")
        ascii_name = fname.encode("ascii", "replace").decode().replace("?", "_")
        disp = "inline" if as_inline else "attachment"
        return Response(
            content=data,
            media_type=row["mime"] if as_inline else "application/octet-stream",
            headers={
                "Content-Disposition": f"{disp}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(fname)}",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'none'; sandbox; frame-ancestors 'none'",
                "Cache-Control": "no-store",
            })

    @app.post("/api/vault/upload")
    async def vault_upload(request: Request):
        """Raw request body (no multipart); metadata in headers/query. Size cap enforced WHILE
        reading, so an oversized body is cut off rather than buffered whole."""
        user = _user(request)
        if not (user and auth.is_admin(user)):
            return JSONResponse({"error": "admin only"}, status_code=403)
        q, h = request.query_params, request.headers

        def meta(name):
            v = h.get("x-vault-" + name) or q.get(name) or ""
            return unquote(v)

        cap = vault.max_file_bytes()
        declared = h.get("content-length")
        if declared and declared.isdigit() and int(declared) > cap:
            return JSONResponse({"error": f"file exceeds the {vault.VAULT_MAX_FILE_MB} MB per-file limit"},
                                status_code=413)
        buf = bytearray()
        async for chunk in request.stream():
            buf.extend(chunk)
            if len(buf) > cap:
                return JSONResponse({"error": f"file exceeds the {vault.VAULT_MAX_FILE_MB} MB per-file limit"},
                                    status_code=413)
        try:
            item = vault.create_item(
                bytes(buf), filename=meta("filename"), folder=meta("folder"), title=meta("title"),
                doc_type=meta("doc-type") or meta("doc_type"), visibility=meta("visibility") or None,
                expires_on=meta("expires-on") or meta("expires_on"),
                review_by=meta("review-by") or meta("review_by"))
        except vault.VaultError as e:
            audit.emit("vault.upload", outcome="failure", request=request, resource="/api/vault/upload",
                       detail={"reason": str(e)}, **_actor(user))
            return _err(e)
        audit.emit("vault.upload", request=request, resource="/api/vault/upload",
                   detail={"item": item["id"], "folder": item["folder"], "size": item["size"],
                           "mime": item["mime"], "visibility": item["visibility"]}, **_actor(user))
        return {"ok": True, "item": item}

    @app.patch("/api/vault/items/{item_id}")
    async def vault_update(item_id: str, request: Request):
        user = _user(request)
        if not (user and auth.is_admin(user)):
            return JSONResponse({"error": "admin only"}, status_code=403)
        body = await request.json()
        if not isinstance(body, dict):
            return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
        fields = {k: body[k] for k in ("title", "folder", "doc_type", "visibility", "expires_on", "review_by")
                  if k in body and isinstance(body[k], str)}
        try:
            item = vault.update_item(item_id, **fields)
        except vault.VaultError as e:
            return _err(e)
        audit.emit("vault.update", request=request, resource=f"/api/vault/items/{item_id}",
                   detail={"item": item_id, "fields": sorted(fields)}, **_actor(user))
        return {"ok": True, "item": item}

    @app.delete("/api/vault/items/{item_id}")
    def vault_delete(item_id: str, request: Request):
        user = _user(request)
        if not (user and auth.is_admin(user)):
            return JSONResponse({"error": "admin only"}, status_code=403)
        try:
            vault.delete_item(item_id)
        except vault.VaultError as e:
            return _err(e)
        audit.emit("vault.delete", request=request, resource=f"/api/vault/items/{item_id}",
                   detail={"item": item_id, "crypto_shredded": True}, **_actor(user))
        return {"ok": True}
