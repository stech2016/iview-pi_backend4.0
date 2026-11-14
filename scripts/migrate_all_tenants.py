"""Apply Alembic migrations to EVERY tenant database in the master registry."""
from sqlalchemy import select

from app.db.provision import provision_schema
from app.db.session import MasterSession, primary_url, mirror_url
from app.models.master import CompanyStorage

with MasterSession() as s:
    for st in s.scalars(select(CompanyStorage).where(CompanyStorage.db_name.is_not(None))):
        for url in filter(None, (primary_url(st), mirror_url(st))):
            print("migrating", st.company_name, url.host, url.database)
            provision_schema(url)
