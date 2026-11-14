from celery import Celery

from app.core.config import settings
from app.core.logging import configure_logging

configure_logging(settings.LOG_LEVEL)

celery_app = Celery("stech", broker=settings.CELERY_BROKER_URL, include=["app.workers.tasks"])
celery_app.conf.update(
    task_acks_late=True,                 # survive worker crashes/restarts
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
    broker_connection_timeout=3,
    task_publish_retry=True,
    task_publish_retry_policy={"max_retries": 2, "interval_start": 0, "interval_step": 0.2, "interval_max": 1},
    timezone="UTC",
    enable_utc=True,
)
