"""One place that tells people about something: phone push + the SMS / voice add-on."""
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.master import User
from app.services import channels, push


def tell(master: Session, company: str, user_ids: list[int], title: str, body: str, data: dict | None = None, severity: str = "major") -> None:
    """Phone push to the given users plus SMS / voice for those who have a phone number set. Never raises."""
    if not user_ids:
        return
    push.notify(company, user_ids, title, body, data)
    people = [{"phone": p} for (p,) in master.execute(select(User.phone).where(User.id.in_(user_ids)))]
    channels.deliver(people, title, body, severity)
