from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from pymongo.errors import DuplicateKeyError

from . import config, db, mailer, security
from .core import client_ip, current_user, user_public

router = APIRouter(prefix="/api")


class SignupIn(BaseModel):
    username: str
    password: str
    email: str


class LoginIn(BaseModel):
    username: str
    password: str


class ForgotIn(BaseModel):
    email: str


class ResetIn(BaseModel):
    token: str
    password: str


class ChangePwIn(BaseModel):
    current_password: str
    new_password: str


def _check_password(pw: str) -> None:
    if len(pw) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    if len(pw) > 128:
        raise HTTPException(400, "Password is too long (max 128)")


def _session(user: dict) -> dict:
    return {"token": security.make_token(user["username"], user.get("token_version", 0)), "user": user_public(user)}


@router.post("/auth/signup")
def signup(body: SignupIn, request: Request) -> dict:
    if not config.SIGNUPS_ENABLED:
        raise HTTPException(403, "Sign ups are closed")
    if not security.limiter.hit(f"signup:{client_ip(request)}", 10, 3600):
        raise HTTPException(429, "Too many sign ups from this network, try again later")
    username = body.username.strip().lower()
    email = body.email.strip().lower()
    if not security.USERNAME_RE.match(username):
        raise HTTPException(400, "Username must be 3-24 characters: letters, numbers or underscore")
    if not security.EMAIL_RE.match(email):
        raise HTTPException(400, "Enter a valid email address")
    _check_password(body.password)
    user = {
        "username": username, "email": email, "pw_hash": security.hash_password(body.password),
        "created_at": db.now(), "token_version": 0, "used_bytes": 0, "disabled": False, "last_login_at": db.now(),
    }
    try:
        db.users.insert_one(user)
    except DuplicateKeyError as exc:
        which = "email" if "email" in str(exc) else "username"
        raise HTTPException(409, f"That {which} is already taken") from exc
    db.spaces.insert_one({
        "_id": db.private_space_id(username), "name": "My Drive", "type": "private",
        "owner": username, "members": [], "created_at": db.now(),
    })
    return _session(user)


@router.post("/auth/login")
def login(body: LoginIn, request: Request) -> dict:
    username = body.username.strip().lower()
    ip = client_ip(request)
    if not (security.limiter.hit(f"login:{ip}:{username}", 8, 600) and security.limiter.hit(f"login-ip:{ip}", 40, 600)):
        raise HTTPException(429, "Too many attempts, wait a few minutes")
    user = db.users.find_one({"username": username})
    if not user:
        security.burn_password_time(body.password)
        raise HTTPException(401, "Wrong username or password")
    if not security.verify_password(body.password, user["pw_hash"]):
        raise HTTPException(401, "Wrong username or password")
    if user.get("disabled"):
        raise HTTPException(403, "This account has been disabled")
    db.users.update_one({"_id": user["_id"]}, {"$set": {"last_login_at": db.now()}})
    return _session(user)


@router.get("/me")
def me(user: dict = Depends(current_user)) -> dict:
    return user_public(user)


@router.post("/auth/forgot")
def forgot(body: ForgotIn, request: Request) -> dict:
    email = body.email.strip().lower()
    ip = client_ip(request)
    if security.limiter.hit(f"forgot:{ip}", 5, 3600) and security.limiter.hit(f"forgot-mail:{email}", 3, 3600):
        user = db.users.find_one({"email": email})
        if user and not user.get("disabled"):
            link = create_reset_link(user["username"])
            mailer.send_mail(
                email, "Reset your GitDrive password",
                f"Hi {user['username']},\n\nUse this link to choose a new password (valid for 30 minutes):\n{link}\n\n"
                "If you didn't ask for this you can ignore this email.",
                f"<p>Hi <b>{user['username']}</b>,</p><p><a href=\"{link}\">Choose a new password</a> (valid for 30 minutes).</p>"
                "<p>If you didn't ask for this you can ignore this email.</p>",
            )
    return {"ok": True}  # same answer whether or not the email exists


def create_reset_link(username: str) -> str:
    from datetime import timedelta

    token = security.new_token(32)
    db.resets.delete_many({"username": username})
    db.resets.insert_one({
        "token_hash": security.sha256_hex(token), "username": username,
        "expires_at": db.now() + timedelta(minutes=30),
    })
    return f"{config.FRONTEND_URL}/reset?token={token}"


@router.post("/auth/reset")
def reset(body: ResetIn, request: Request) -> dict:
    if not security.limiter.hit(f"reset:{client_ip(request)}", 20, 3600):
        raise HTTPException(429, "Too many attempts")
    _check_password(body.password)
    rec = db.resets.find_one({"token_hash": security.sha256_hex(body.token)})
    if not rec or rec["expires_at"].replace(tzinfo=rec["expires_at"].tzinfo or db.now().tzinfo) < db.now():
        raise HTTPException(400, "This reset link is invalid or has expired")
    db.users.update_one({"username": rec["username"]},
                        {"$set": {"pw_hash": security.hash_password(body.password)}, "$inc": {"token_version": 1}})
    db.resets.delete_many({"username": rec["username"]})
    return {"ok": True}


@router.post("/me/password")
def change_password(body: ChangePwIn, user: dict = Depends(current_user)) -> dict:
    if not security.verify_password(body.current_password, user["pw_hash"]):
        raise HTTPException(400, "Current password is wrong")
    _check_password(body.new_password)
    updated = db.users.find_one_and_update(
        {"_id": user["_id"]},
        {"$set": {"pw_hash": security.hash_password(body.new_password)}, "$inc": {"token_version": 1}},
        return_document=True,
    )
    return _session(updated)  # new token; every other session is signed out
