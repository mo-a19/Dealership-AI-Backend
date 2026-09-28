import re
import secrets

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel
from supabase import create_client

from app.config import get_settings

_s = get_settings()
_anon = create_client(_s.supabase_url, _s.supabase_anon_key)
_svc  = create_client(_s.supabase_url, _s.supabase_service_key)

router = APIRouter(tags=["Auth"])


def _slugify(name: str) -> str:
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    return slug.strip("-")


def _unique_slug(name: str) -> str:
    base = _slugify(name)
    slug = base
    while True:
        exists = _svc.table("organizations").select("id").eq("slug", slug).execute()
        if not exists.data:
            return slug
        slug = f"{base}-{secrets.token_hex(3)}"


class RegisterIn(BaseModel):
    email: str
    password: str
    full_name: str
    dealership_name: str


class LoginIn(BaseModel):
    email: str
    password: str


class RefreshIn(BaseModel):
    refresh_token: str


def _resp(session, user):
    m = user.app_metadata
    return {
        "access_token": session.access_token,
        "refresh_token": session.refresh_token,
        "token_type": "bearer",
        "user": {"id": str(user.id), "email": user.email, "org_id": m.get("org_id"), "role": m.get("role")},
    }


@router.post("/auth/register", status_code=201)
def register(req: RegisterIn):
    slug = _unique_slug(req.dealership_name)
    org = _svc.table("organizations").insert({
        "name": req.dealership_name,
        "slug": slug,
    }).execute()
    if not org.data:
        raise HTTPException(400, "Failed to create organization.")
    org_id = org.data[0]["id"]

    # Set pinecone_namespace now that we have the org_id
    _svc.table("organizations").update({
        "pinecone_namespace": str(org_id),
    }).eq("id", org_id).execute()
    try:
        auth_user = _svc.auth.admin.create_user({
            "email": req.email,
            "password": req.password,
            "email_confirm": True,
            "user_metadata": {"full_name": req.full_name},
            "app_metadata": {"org_id": str(org_id), "role": "admin"},
        })
    except Exception as exc:
        _svc.table("organizations").delete().eq("id", org_id).execute()
        raise HTTPException(400, str(exc))

    try:
        _svc.table("users").insert({
            "id": str(auth_user.user.id),
            "org_id": str(org_id),
            "email": req.email,
            "full_name": req.full_name,
            "role": "admin",
        }).execute()
    except Exception as exc:
        _svc.auth.admin.delete_user(str(auth_user.user.id))
        _svc.table("organizations").delete().eq("id", org_id).execute()
        raise HTTPException(400, f"Failed to create user profile: {exc}")

    resp = _anon.auth.sign_in_with_password({"email": req.email, "password": req.password})
    return _resp(resp.session, resp.user)


@router.post("/auth/login")
def login(req: LoginIn):
    try:
        resp = _anon.auth.sign_in_with_password({"email": req.email, "password": req.password})
    except Exception:
        raise HTTPException(401, "Invalid credentials.")
    return _resp(resp.session, resp.user)


@router.post("/auth/logout", status_code=204)
def logout(authorization: str = Header(...)):
    token = authorization.removeprefix("Bearer ").strip()
    try:
        _svc.auth.admin.sign_out(token)
    except Exception:
        pass


@router.post("/auth/refresh")
def refresh(req: RefreshIn):
    try:
        resp = _anon.auth.refresh_session(req.refresh_token)
    except Exception:
        raise HTTPException(401, "Invalid or expired refresh token.")
    return _resp(resp.session, resp.user)


@router.get("/auth/me")
def me(authorization: str = Header(...)):
    token = authorization.removeprefix("Bearer ").strip()
    try:
        resp = _svc.auth.get_user(token)
    except Exception:
        raise HTTPException(401, "Invalid or expired token.")
    return {
        "id": str(resp.user.id),
        "email": resp.user.email,
        "full_name": resp.user.user_metadata.get("full_name"),
        "org_id": resp.user.app_metadata.get("org_id"),
        "role": resp.user.app_metadata.get("role"),
    }


class ForgotPasswordIn(BaseModel):
    email: str


class ResetPasswordIn(BaseModel):
    access_token: str
    new_password: str


@router.post("/auth/forgot-password", status_code=200)
def forgot_password(req: ForgotPasswordIn):
    s = get_settings()
    redirect_to = f"{s.frontend_url}/reset-password"
    try:
        _anon.auth.reset_password_for_email(req.email, {"redirect_to": redirect_to})
    except Exception:
        pass  # never reveal whether the email exists
    return {"message": "If that email is registered, a reset link has been sent."}


@router.post("/auth/reset-password", status_code=200)
def reset_password(req: ResetPasswordIn):
    try:
        user_resp = _svc.auth.get_user(req.access_token)
        _svc.auth.admin.update_user_by_id(
            str(user_resp.user.id),
            {"password": req.new_password},
        )
    except Exception:
        raise HTTPException(400, "Invalid or expired reset token.")
    return {"message": "Password updated successfully."}
