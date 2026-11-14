from typing import Literal

from pydantic import BaseModel, Field

Mode = Literal["local", "cloud"]   # hybrid needs Celery / RabbitMQ, which this deployment does not run


class DbListIn(BaseModel):
    mode: Mode = "local"
    cloud_host: str | None = Field(default=None, max_length=255)
    cloud_port: int | None = Field(default=5432, ge=1, le=65535)
    cloud_user: str | None = Field(default=None, max_length=128)
    cloud_password: str | None = Field(default=None, max_length=256)


class StorageIn(DbListIn):
    db_name: str
    create_if_missing: bool = False


class StorageOut(BaseModel):
    configured: bool
    mode: str | None = None
    db_name: str | None = None
    cloud_host: str | None = None
    cloud_port: int | None = None
    cloud_user: str | None = None


class DbListOut(BaseModel):
    databases: list[str]
    suggested_new_name: str


class StorageSavedOut(BaseModel):
    mode: str
    db_name: str
    created: bool
    message: str
