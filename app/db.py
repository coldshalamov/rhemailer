"""Database utilities for rh-emailer."""

from __future__ import annotations

import json
import os
import uuid
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Dict, Generator, Optional

from sqlalchemy import Column, DateTime, String, Text, create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, declarative_base, sessionmaker

DATABASE_URL = os.getenv("DB_PATH") or os.getenv("DATABASE_URL", "sqlite:////tmp/emailer.db")

Base = declarative_base()
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


class Job(Base):
    __tablename__ = "jobs"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    status = Column(String, nullable=False, default="pending")
    payload = Column(Text, nullable=True)
    result = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    def update_timestamp(self) -> None:
        self.updated_at = datetime.utcnow()

    def set_payload(self, data: Dict[str, Any]) -> None:
        self.payload = json.dumps(data)

    def get_payload(self) -> Optional[Dict[str, Any]]:
        if not self.payload:
            return None
        return json.loads(self.payload)

    def set_result(self, data: Dict[str, Any]) -> None:
        self.result = json.dumps(data)

    def get_result(self) -> Optional[Dict[str, Any]]:
        if not self.result:
            return None
        return json.loads(self.result)


class Suppression(Base):
    __tablename__ = "suppression"

    email = Column(String, primary_key=True)
    added_at = Column(DateTime, default=datetime.utcnow, nullable=False)


def init_db() -> None:
    Base.metadata.create_all(bind=engine)


@contextmanager
def get_session() -> Generator[Session, None, None]:
    session: Session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def create_job(session: Session, payload: Dict[str, Any]) -> Job:
    job = Job()
    job.set_payload(payload)
    session.add(job)
    session.flush()
    return job


def update_job_status(
    session: Session, job_id: str, status: str, result: Optional[Dict[str, Any]] = None
) -> Optional[Job]:
    job: Optional[Job] = session.get(Job, job_id)
    if job is None:
        return None
    job.status = status
    job.update_timestamp()
    if result is not None:
        job.set_result(result)
    session.add(job)
    session.flush()
    return job


def get_job(session: Session, job_id: str) -> Optional[Job]:
    return session.get(Job, job_id)


def add_to_suppression(session: Session, email: str) -> bool:
    record = Suppression(email=email.lower())
    session.add(record)
    try:
        session.flush()
        return True
    except IntegrityError:
        session.rollback()
        return False


def is_suppressed(session: Session, email: str) -> bool:
    return session.get(Suppression, email.lower()) is not None
