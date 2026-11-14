"""Master session + per-tenant engine cache (LRU, capped pools)."""
from collections import OrderedDict
from contextlib import contextmanager
from threading import Lock

from sqlalchemy import create_engine, select
from sqlalchemy.engine import URL, Engine, make_url
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.core.crypto import decrypt
from app.core.errors import AppError

PG_CONNECT_ARGS = {"options": "-c timezone=utc", "connect_timeout": 5}  # UTC discipline

master_engine = create_engine(
    settings.master_url, pool_size=10, max_overflow=5, pool_recycle=1800, pool_pre_ping=True,
    connect_args=PG_CONNECT_ARGS,
)
MasterSession = sessionmaker(master_engine, expire_on_commit=False)


def get_master_db():
    with MasterSession() as session:
        yield session


class EngineCache:
    """LRU cache of tenant engines. Each engine: pool_size=5, max_overflow=2, recycle 30 min."""

    def __init__(self, max_size: int = 64):
        self.max_size = max_size
        self._engines: "OrderedDict[tuple[str, str], tuple[str, Engine]]" = OrderedDict()
        self._lock = Lock()

    def get(self, company: str, role: str, url: URL) -> Engine:
        key = (company, role)
        url_s = url.render_as_string(hide_password=False)
        with self._lock:
            entry = self._engines.get(key)
            if entry and entry[0] == url_s:
                self._engines.move_to_end(key)
                return entry[1]
            if entry:  # config changed (possibly in another worker) -> rebuild
                entry[1].dispose()
            engine = create_engine(
                url, pool_size=5, max_overflow=2, pool_recycle=1800, pool_pre_ping=True,
                connect_args=PG_CONNECT_ARGS,
            )
            self._engines[key] = (url_s, engine)
            self._engines.move_to_end(key)
            while len(self._engines) > self.max_size:
                _, (_, old) = self._engines.popitem(last=False)
                old.dispose()
            return engine

    def clear(self, company: str) -> None:
        with self._lock:
            for key in [k for k in self._engines if k[0] == company]:
                self._engines.pop(key)[1].dispose()


engine_cache = EngineCache()


def cloud_url(host: str, port: int, user: str, password: str, database: str) -> URL:
    return URL.create("postgresql+psycopg2", username=user, password=password, host=host, port=port, database=database)


def primary_url(storage) -> URL:
    if storage.mode == "cloud":
        return cloud_url(storage.cloud_host, storage.cloud_port, storage.cloud_user,
                         decrypt(storage.encrypted_cloud_password), storage.db_name)
    return make_url(decrypt(storage.encrypted_local_url))


def mirror_url(storage) -> URL | None:
    if storage.mode != "hybrid":
        return None
    return cloud_url(storage.cloud_host, storage.cloud_port, storage.cloud_user,
                     decrypt(storage.encrypted_cloud_password), storage.db_name)


def tenant_session_for_storage(storage) -> Session:
    if not storage or not storage.db_name:
        raise AppError(409, "STORAGE_NOT_CONFIGURED",
                       "No database is configured for your company yet. Ask an administrator to open Storage settings and choose a database.")
    engine = engine_cache.get(storage.company_name, "primary", primary_url(storage))
    session = Session(engine, expire_on_commit=False)
    session.info["company"] = storage.company_name
    session.info["hybrid"] = storage.mode == "hybrid"
    return session


def open_tenant_session(master: Session, company: str) -> Session:
    from app.models.master import CompanyStorage

    storage = master.scalar(select(CompanyStorage).where(CompanyStorage.company_name == company))
    return tenant_session_for_storage(storage)


@contextmanager
def tenant_session(master: Session, company: str):
    session = open_tenant_session(master, company)
    try:
        yield session
    finally:
        session.close()
