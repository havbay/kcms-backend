from typing import Protocol
from urllib.parse import urlencode

import httpx
from fastapi import HTTPException, status

from kcms.integrations.contracts import ProviderPage
from kcms.settings import settings


class MetaClient(Protocol):
    async def validate_page_token(self, token: str) -> ProviderPage: ...
    def authorization_url(self, state: str) -> str: ...
    async def exchange_code(self, code: str) -> str: ...
    async def list_pages(self, user_token: str) -> list[ProviderPage]: ...


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

    def authorization_url(self, state: str) -> str:
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
        body = await self._get(
            "me", {"fields": "id,name,tasks", "access_token": token}
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


def get_meta_client() -> MetaClient:
    if not all(
        (
            settings.meta_graph_version,
            settings.meta_app_id,
            settings.meta_app_secret,
            settings.meta_login_config_id,
            settings.meta_oauth_redirect_uri,
        )
    ):
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
