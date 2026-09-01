from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, EmailStr, Field

from kcms.auth import repository
from kcms.auth.security import verify_telegram_payload
from kcms.settings import settings
from kcms.shared.database import database

router = APIRouter(prefix="/api/v1/auth")


class SignUpRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=200)
    display_name: str = Field(default="", max_length=80)
    organization: str = Field(default="", max_length=120)


class SignInRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=200)


class TelegramRequest(BaseModel):
    # Verbatim Telegram Login Widget payload, including its hash.
    payload: dict[str, str]


class AuthUser(BaseModel):
    id: str
    display_name: str
    # Lets the frontend decide whether to offer administration navigation.
    # Authorization is still enforced per request; this is presentation only.
    is_platform_admin: bool = False


class Session(BaseModel):
    token: str
    user: AuthUser


class Providers(BaseModel):
    email: bool
    telegram: bool
    telegram_bot_username: str | None


def _as_auth_user(user: dict[str, Any]) -> "AuthUser":
    return AuthUser(
        id=user["id"],
        display_name=user["display_name"],
        is_platform_admin=bool(user.get("is_platform_admin")),
    )


def _require_database() -> None:
    if not database.connected:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "database unavailable")


async def current_user(
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Bearer-token guard. Used instead of cookies because the frontend and API
    are on different sites, where SameSite=None cookies are blocked by default
    in several browsers."""
    _require_database()
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "authentication required")
    token = authorization.split(" ", 1)[1].strip()
    async with database.acquire() as connection:
        user = await repository.user_for_token(connection, token)
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "session is invalid or expired")
    return user


@router.get("/providers", operation_id="listAuthProviders", response_model=Providers)
async def list_providers() -> Providers:
    enabled = bool(settings.telegram_bot_token and settings.telegram_bot_username)
    return Providers(
        email=True,
        telegram=enabled,
        telegram_bot_username=settings.telegram_bot_username or None,
    )


@router.post(
    "/signup",
    operation_id="signUp",
    response_model=Session,
    status_code=201,
    include_in_schema=False,
)
async def sign_up(body: SignUpRequest) -> Session:
    if not settings.public_signup_enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    _require_database()
    async with database.acquire() as connection:
        created = await repository.sign_up_with_email(
            connection, str(body.email), body.password, body.display_name, body.organization
        )
    if created is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "that email is already registered")
    token, user = created
    return Session(token=token, user=_as_auth_user(user))


@router.post("/signin", operation_id="signIn", response_model=Session)
async def sign_in(body: SignInRequest) -> Session:
    _require_database()
    async with database.acquire() as connection:
        found = await repository.sign_in_with_email(connection, str(body.email), body.password)
    if found is None:
        # One message for both causes: distinguishing them enumerates accounts.
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "email or password is incorrect")
    token, user = found
    return Session(token=token, user=_as_auth_user(user))


@router.post("/telegram", operation_id="signInWithTelegram", response_model=Session)
async def sign_in_with_telegram(body: TelegramRequest) -> Session:
    _require_database()
    if not settings.telegram_bot_token:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "telegram sign-in is not enabled")
    if not verify_telegram_payload(body.payload, settings.telegram_bot_token):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "telegram payload failed verification")

    telegram_id = body.payload.get("id", "")
    display_name = " ".join(
        part for part in (body.payload.get("first_name"), body.payload.get("last_name")) if part
    ) or body.payload.get("username") or f"telegram-{telegram_id}"

    async with database.acquire() as connection:
        token, user = await repository.sign_in_with_telegram(connection, telegram_id, display_name)
    return Session(token=token, user=_as_auth_user(user))


@router.get("/me", operation_id="getCurrentUser", response_model=AuthUser)
async def get_current_user(user: Annotated[dict[str, Any], Depends(current_user)]) -> AuthUser:
    return _as_auth_user(user)


@router.post("/signout", operation_id="signOut", status_code=204)
async def sign_out(authorization: Annotated[str | None, Header()] = None) -> None:
    _require_database()
    if authorization and authorization.lower().startswith("bearer "):
        async with database.acquire() as connection:
            await repository.revoke_session(connection, authorization.split(" ", 1)[1].strip())
