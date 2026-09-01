from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class ProviderPage:
    page_id: str
    page_name: str
    access_token: str
    tasks: tuple[str, ...] = ()

    @property
    def can_moderate(self) -> bool:
        return bool(
            {"PROFILE_PLUS_MODERATE", "PROFILE_PLUS_MANAGE", "PROFILE_PLUS_FULL_CONTROL"}
            .intersection(self.tasks)
        )


@dataclass(frozen=True)
class ProviderComment:
    """One comment as the provider reports it.

    `author_ref` is a display name when Meta supplies one. Meta withholds
    `from` for commenters who have not authorized the app, which is the normal
    case on a real Page, so this falls back to the comment id rather than
    inventing an identity.
    """

    comment_id: str
    text: str
    created_time: datetime
    author_ref: str
    post_text: str | None = None
    post_permalink: str | None = None
    post_kind: str = "UNKNOWN"
    parent_text: str | None = None
    is_reply: bool = False
