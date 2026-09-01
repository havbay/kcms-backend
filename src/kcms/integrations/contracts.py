from dataclasses import dataclass


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
