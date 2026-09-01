from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import urlencode

import httpx
from fastapi import HTTPException, status

from kcms.integrations.contracts import ProviderComment, ProviderPage
from kcms.settings import settings


class MetaClient(Protocol):
    async def validate_page_token(self, token: str) -> ProviderPage: ...
    def authorization_url(self, state: str) -> str: ...
    async def exchange_code(self, code: str) -> str: ...
    async def list_pages(self, user_token: str) -> list[ProviderPage]: ...
    async def fetch_comments(self, page_id: str, token: str) -> list[ProviderComment]: ...
    async def set_comment_hidden(self, comment_id: str, token: str, hidden: bool) -> None: ...


class GraphMetaClient:
    def __init__(
        self,
        graph_version: str,
        app_id: str,
        app_secret: str,
        redirect_uri: str,
        scopes: str,
        login_config_id: str,
    ):
        self._graph_version = graph_version
        self._app_id = app_id
        self._app_secret = app_secret
        self._redirect_uri = redirect_uri
        self._scopes = scopes
        self._login_config_id = login_config_id

    def _require_oauth(self) -> None:
        """Facebook Login needs app credentials and a redirect target. Reading
        and moderating comments with a Page token does not, so only the login
        flow is refused when they are absent."""
        if not all(
            (self._app_id, self._app_secret, self._redirect_uri, self._login_config_id)
        ):
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "Facebook Login is not configured on this deployment",
            )

    def authorization_url(self, state: str) -> str:
        self._require_oauth()
        query = urlencode(
            {
                "client_id": self._app_id,
                "redirect_uri": self._redirect_uri,
                "state": state,
                "scope": self._scopes,
                "config_id": self._login_config_id,
                "response_type": "code",
            }
        )
        return f"https://www.facebook.com/{self._graph_version}/dialog/oauth?{query}"

    async def _get(self, path: str, params: dict[str, str]) -> dict:
        url = f"https://graph.facebook.com/{self._graph_version}/{path.lstrip('/')}"
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.get(url, params=params)
        except httpx.RequestError as exc:
            raise ValueError("Meta could not be reached") from exc
        if response.status_code != 200:
            raise ValueError("Meta rejected the authorization")
        return response.json()

    async def validate_page_token(self, token: str) -> ProviderPage:
        """Identify the Page a token belongs to, or say why it is the wrong token.

        A User token also answers /me with an id and a name, so without the node
        type a user token would be stored as though it were a Page. `tasks` is
        requested but not required: some tokens cannot read that field, and
        failing the whole connection over a missing capability list would be
        worse than connecting with an empty one.
        """
        params = {"fields": "id,name,tasks", "metadata": "1", "access_token": token}
        try:
            body = await self._get("me", params)
        except ValueError:
            body = await self._get(
                "me", {"fields": "id,name", "metadata": "1", "access_token": token}
            )

        node_type = str((body.get("metadata") or {}).get("type", "")).lower()
        if node_type and node_type != "page":
            raise ValueError(
                "This is a User access token, not a Page access token. In Graph "
                "API Explorer open the 'User or Page' menu and choose your Page "
                "under Page Access Tokens, then copy the token again."
            )

        page_id, page_name = body.get("id"), body.get("name")
        if not page_id or not page_name:
            raise ValueError("Meta did not identify a Facebook Page")
        return ProviderPage(
            page_id=str(page_id),
            page_name=str(page_name),
            access_token=token,
            tasks=tuple(str(task) for task in body.get("tasks", [])),
        )

    async def exchange_code(self, code: str) -> str:
        self._require_oauth()
        body = await self._get(
            "oauth/access_token",
            {
                "client_id": self._app_id,
                "client_secret": self._app_secret,
                "redirect_uri": self._redirect_uri,
                "code": code,
            },
        )
        token = body.get("access_token")
        if not token:
            raise ValueError("Meta did not return an access token")
        return str(token)

    async def list_pages(self, user_token: str) -> list[ProviderPage]:
        body = await self._get(
            "me/accounts",
            {"fields": "id,name,access_token,tasks", "access_token": user_token},
        )
        pages: list[ProviderPage] = []
        for item in body.get("data", []):
            if item.get("id") and item.get("name") and item.get("access_token"):
                pages.append(
                    ProviderPage(
                        page_id=str(item["id"]),
                        page_name=str(item["name"]),
                        access_token=str(item["access_token"]),
                        tasks=tuple(str(task) for task in item.get("tasks", [])),
                    )
                )
        return pages


    async def _post(self, path: str, params: dict[str, str]) -> dict:
        url = f"https://graph.facebook.com/{self._graph_version}/{path.lstrip('/')}"
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.post(url, data=params)
        except httpx.RequestError as exc:
            raise ValueError("Meta could not be reached") from exc
        if response.status_code != 200:
            # Meta explains refusals in the body; surface it so an operator can
            # tell a missing permission from an expired token.
            raise ValueError(f"Meta rejected the request: {response.text[:200]}")
        return response.json()

    @staticmethod
    def _post_kind(post: dict[str, Any]) -> str:
        media = ""
        for attachment in post.get("attachments", {}).get("data", []):
            media = str(attachment.get("media_type", "")).lower()
            break
        if media == "photo":
            return "IMAGE"
        if media == "video":
            return "VIDEO"
        return "TEXT" if post.get("message") else "UNKNOWN"

    @staticmethod
    def _parse_time(value: str | None) -> datetime:
        if not value:
            return datetime.now(UTC)
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return datetime.now(UTC)

    async def fetch_comments(self, page_id: str, token: str) -> list[ProviderComment]:
        """Read comments on the Page's recent posts.

        Meta returns comments nested under each post, so one request covers the
        whole window. `from` is absent for commenters who have not authorized
        the app - the ordinary case on a real Page - so the author falls back to
        the comment id rather than being invented.
        """
        body = await self._get(
            f"{page_id}/feed",
            {
                "fields": (
                    "id,message,created_time,permalink_url,attachments{media_type},"
                    "comments.limit(100){id,message,created_time,from,parent{id,message}}"
                ),
                "limit": "25",
                "access_token": token,
            },
        )
        comments: list[ProviderComment] = []
        for post in body.get("data", []):
            post_text = post.get("message")
            post_permalink = post.get("permalink_url")
            post_kind = self._post_kind(post)
            for item in post.get("comments", {}).get("data", []):
                comment_id, message = item.get("id"), item.get("message")
                if not comment_id or not message:
                    # A sticker- or photo-only comment carries no text to
                    # classify. Skipping keeps the work list reviewable.
                    continue
                parent = item.get("parent") or {}
                author = (item.get("from") or {}).get("name")
                comments.append(
                    ProviderComment(
                        comment_id=str(comment_id),
                        text=str(message),
                        created_time=self._parse_time(item.get("created_time")),
                        author_ref=str(author) if author else f"fb:{comment_id}",
                        post_text=str(post_text) if post_text else None,
                        post_permalink=str(post_permalink) if post_permalink else None,
                        post_kind=post_kind,
                        parent_text=str(parent["message"]) if parent.get("message") else None,
                        is_reply=bool(parent),
                    )
                )
        return comments

    async def set_comment_hidden(self, comment_id: str, token: str, hidden: bool) -> None:
        """Hide or unhide one comment on the Page itself."""
        await self._post(
            comment_id, {"is_hidden": "true" if hidden else "false", "access_token": token}
        )


def get_meta_client() -> MetaClient:
    # Only the Graph version is required to talk to Meta at all. Connecting a
    # Page with its own token, reading comments and hiding them need nothing
    # more; Facebook Login additionally checks its own settings when used.
    if not settings.meta_graph_version:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Meta Graph API is not configured",
        )
    return GraphMetaClient(
        settings.meta_graph_version,
        settings.meta_app_id,
        settings.meta_app_secret,
        settings.meta_oauth_redirect_uri,
        settings.meta_oauth_scopes,
        settings.meta_login_config_id,
    )


def get_optional_meta_client() -> MetaClient | None:
    """The Graph client when the deployment has one, otherwise None.

    Moderating seeded sample comments must keep working on a deployment with
    no Meta credentials, so callers that only sometimes reach the provider
    depend on this rather than failing the whole request.
    """
    try:
        return get_meta_client()
    except HTTPException:
        return None
