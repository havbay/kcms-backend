from typing import Literal

Plan = Literal["TRIAL", "STARTER", "GROWTH"]

# Enterprise is a custom annual quote handled outside the product, so it has
# no page limit here — a workspace never actually holds that plan value yet.
PLAN_PAGE_LIMITS: dict[Plan, int] = {"TRIAL": 1, "STARTER": 3, "GROWTH": 10}
