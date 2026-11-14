from pydantic import BaseModel


class RemoteAccessIn(BaseModel):
    funnel_url: str


class RemoteAccessOut(BaseModel):
    funnel_url: str | None = None
    api_base_url: str | None = None
    source: str  # "company" | "server_default" | "none"


class RemoteTestOut(BaseModel):
    reachable: bool
    detail: str
