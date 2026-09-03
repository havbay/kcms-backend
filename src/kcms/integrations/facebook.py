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

    async def delete_comment(self, comment_id: str, token: str) -> None: ...


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
        """Facebook Login needs app credentials and a redirect target.

        The login configuration id is not required: it selects a Facebook Login
        for Business configuration, and without one Meta runs classic Facebook
        Login against the requested scopes. Demanding it refused deployments
        that could have completed the flow.
        """
        if not all((self._app_id, self._app_secret, self._redirect_uri)):
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "Facebook Login is not configured on this deployment",
            )

    def authorization_url(self, state: str) -> str:
        self._require_oauth()
        params = {
            "client_id": self._app_id,
            "redirect_uri": self._redirect_uri,
            "state": state,
            "response_type": "code",
        }
        if self._login_config_id:
            # A Business Login configuration carries its own permission set;
            # sending scope alongside it is ignored, so it is left out to keep
            # the authorization screen matching the configuration.
            params["config_id"] = self._login_config_id
        else:
            params["scope"] = self._scopes
        return (
            f"https://www.facebook.com/{self._graph_version}/dialog/oauth?"
            + urlencode(params)
        )

    async def _get(self, path: str, params: dict[str, str]) -> dict:
        url = f"https://graph.facebook.com/{self._graph_version}/{path.lstrip('/')}"
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.get(url, params=params)
        except httpx.RequestError as exc:
            raise ValueError("Meta could not be reached") from exc
        if response.status_code != 200:
            # Meta names the missing permission or expired token in the body.
            # Discarding it left every failure indistinguishable.
            raise ValueError(f"Meta rejected the request: {response.text[:200]}")
        return response.json()

    async def validate_page_token(self, token: str) -> ProviderPage:
        """Identify the Page a token belongs to, or say why it is the wrong token.

        Identity comes from debug_token rather than /me. A User token answers
        /me with an id and a name just like a Page token, so /me cannot tell
        them apart; and reading the Page node needs pages_read_engagement,
        which a token can lack while still being able to read comments and
        hide them. debug_token needs no Page permission at all and states the
        token type outright.
        """
        debug = await self._get(
            "debug_token", {"input_token": token, "access_token": token}
        )
        data = debug.get("data") or {}
        token_type = str(data.get("type", "")).upper()
        if token_type != "PAGE":
            raise ValueError(
                "This is a "
                + (token_type.capitalize() or "non-Page")
                + " access token, not a Page access token. In Graph API Explorer "
                "open the 'User or Page' menu and choose your Page under Page "
                "Access Tokens, then copy the token again."
            )
        page_id = data.get("profile_id")
        if not page_id:
            raise ValueError("Meta did not identify a Facebook Page")

        # The Page's own name and task list are a nicety: reading them needs
        # pages_read_engagement, and a token without it can still moderate.
        page_name, tasks = "", ()
        try:
            profile = await self._get(
                str(page_id), {"fields": "name,tasks", "access_token": token}
            )
            page_name = str(profile.get("name") or "")
            tasks = tuple(str(task) for task in profile.get("tasks", []))
        except ValueError:
            pass

        # Meta withholds the task list from a token that cannot read the Page.
        # Hiding a comment needs BOTH scopes: pages_manage_engagement alone is
        # refused with "(#200) Requires pages_read_engagement permission to
        # manage the object", so claiming moderation on the manage scope alone
        # would promise an action that fails at the moment it matters.
        scopes = set(data.get("scopes") or [])
        if not tasks and {"pages_manage_engagement", "pages_read_engagement"} <= scopes:
            tasks = ("PROFILE_PLUS_MODERATE",)

        return ProviderPage(
            page_id=str(page_id),
            page_name=page_name or f"Facebook Page {page_id}",
            access_token=token,
            tasks=tasks,
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


    async def _delete(self, path: str, params: dict[str, str]) -> dict:
        url = f"https://graph.facebook.com/{self._graph_version}/{path.lstrip('/')}"
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.delete(url, params=params)
        except httpx.RequestError as exc:
            raise ValueError("Meta could not be reached") from exc
        if response.status_code != 200:
            raise ValueError(f"Meta rejected the request: {response.text[:200]}")
        return response.json()

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

        Comments are read from each post's own edge rather than nested inside
        the feed request. Nesting `comments{...}` into /feed makes the whole
        call require pages_read_engagement, while /{post-id}/comments does not,
        so this works with a Page token carrying only pages_read_user_content -
        what Graph API Explorer commonly produces.

        `from` is absent for commenters who have not authorized the app, the
        ordinary case on a real Page, so the author falls back to the comment
        id rather than being invented.
        """
        # attachments{media_type} is what distinguishes a video post from a
        # text one, and it alone makes /feed require pages_read_engagement.
        # It only labels the post, so a token without that permission reads
        # the same comments and loses only the label.
        try:
            feed = await self._get(
                f"{page_id}/feed",
                {
                    "fields": (
                        "id,message,created_time,permalink_url,attachments{media_type}"
                    ),
                    "limit": "25",
                    "access_token": token,
                },
            )
        except ValueError:
            feed = await self._get(
                f"{page_id}/feed",
                {
                    "fields": "id,message,created_time,permalink_url",
                    "limit": "25",
                    "access_token": token,
                },
            )

        comments: list[ProviderComment] = []
        for post in feed.get("data", []):
            post_id = post.get("id")
            if not post_id:
                continue
            post_text = post.get("message")
            post_permalink = post.get("permalink_url")
            post_kind = self._post_kind(post)
            try:
                body = await self._get(
                    f"{post_id}/comments",
                    {
                        "fields": "id,message,created_time,from,parent{id,message}",
                        "limit": "100",
                        "access_token": token,
                    },
                )
            except ValueError:
                # One unreadable post must not lose the comments on every
                # other post in the window.
                continue
            for item in body.get("data", []):
                comment_id, message = item.get("id"), item.get("message")
                if not comment_id or not message:
                    # A sticker- or photo-only comment carries no text to
                    # classify. Skipping keeps the work list reviewable.
                    continue
                parent = item.get("parent") or {}
                sender = item.get("from") or {}
                author = sender.get("name")
                comments.append(
                    ProviderComment(
                        comment_id=str(comment_id),
                        text=str(message),
                        created_time=self._parse_time(item.get("created_time")),
                        author_ref=str(author) if author else f"fb:{comment_id}",
                        author_id=str(sender["id"]) if sender.get("id") else None,
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
        try:
            await self._post(
                comment_id,
                {"is_hidden": "true" if hidden else "false", "access_token": token},
            )
        except ValueError as exc:
            # Facebook refuses to hide a comment the Page itself wrote, and
            # reports it as a bare "(#200) Can not hide or unhide this
            # comment". Passing that through leaves an operator checking
            # permissions that were never the problem.
            if "Can not hide or unhide" in str(exc):
                raise ValueError(
                    "Facebook will not hide this comment. A Page cannot hide "
                    "its own comments — this one was posted by the Page rather "
                    "than by a visitor."
                ) from exc
            raise


    async def delete_comment(self, comment_id: str, token: str) -> None:
        """Remove one comment from the Page permanently.

        There is no undo: unlike hiding, nothing on Facebook's side keeps the
        text. The action row in KCMS is the only record left that it existed,
        which is why the action is written before this call is made.
        """
        try:
            await self._delete(comment_id, {"access_token": token})
        except ValueError as exc:
            # A comment already gone is the outcome the caller wanted, so a
            # missing object is not reported as a failure.
            if "does not exist" in str(exc) or "Unsupported get request" in str(exc):
                return
            raise


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
