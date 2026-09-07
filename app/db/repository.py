from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import DateTime, Integer, String, Text, UniqueConstraint, create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from app.models import Feedback, Job, JobStatus, validate_transition


class Base(DeclarativeBase):
    pass


class JobRecord(Base):
    __tablename__ = "jobs"
    __table_args__ = (UniqueConstraint("source", "dedupe_key", name="uq_job_source_dedupe"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    source_job_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    dedupe_key: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    final_score: Mapped[float | None] = mapped_column(nullable=True, index=True)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class FeedbackRecord(Base):
    __tablename__ = "feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    feedback: Mapped[str] = mapped_column(String(30), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(250), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


class NotificationRecord(Base):
    __tablename__ = "job_notifications"
    __table_args__ = (UniqueConstraint("job_id", "channel", "analyzer_version", name="uq_job_notification"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    channel: Mapped[str] = mapped_column(String(30), nullable=False)
    analyzer_version: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    error: Mapped[str | None] = mapped_column(String(300), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class JobActionRecord(Base):
    __tablename__ = "job_actions"
    __table_args__ = (UniqueConstraint("job_id", "action", "analyzer_version", name="uq_job_action"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(30), nullable=False)
    analyzer_version: Mapped[str] = mapped_column(String(50), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DraftRequestRecord(Base):
    __tablename__ = "draft_requests"
    __table_args__ = (UniqueConstraint("job_id", "analyzer_version", name="uq_job_draft_request"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    analyzer_version: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    draft: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(String(300), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ProcessingErrorRecord(Base):
    __tablename__ = "processing_errors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    stage: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    error_kind: Mapped[str] = mapped_column(String(80), nullable=False)
    message: Mapped[str] = mapped_column(String(300), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RSSFeedStateRecord(Base):
    __tablename__ = "rss_feed_state"

    feed_url: Mapped[str] = mapped_column(String(500), primary_key=True)
    source: Mapped[str] = mapped_column(String(40), nullable=False)
    feed_name: Mapped[str] = mapped_column(String(150), nullable=False)
    baseline_identity_count: Mapped[int] = mapped_column(Integer, nullable=False)
    onboarded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RSSFeedBaselineIdentityRecord(Base):
    __tablename__ = "rss_feed_baseline_identities"
    __table_args__ = (
        UniqueConstraint("feed_url", "source", "source_identity", name="uq_feed_baseline_identity"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    feed_url: Mapped[str] = mapped_column(String(500), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    source_identity: Mapped[str] = mapped_column(String(600), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


def _normalize_database_url(url: str) -> str:
    if url.startswith("sqlite:///./"):
        return "sqlite:///" + str(Path.cwd() / url.removeprefix("sqlite:///./"))
    return url


class JobRepository:
    def __init__(self, database_url: str):
        self.engine = create_engine(_normalize_database_url(database_url), connect_args={"check_same_thread": False} if database_url.startswith("sqlite") else {})
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)

    def initialize(self) -> None:
        Base.metadata.create_all(self.engine)

    def exists(self, source: str, dedupe_key: str) -> bool:
        with self.sessions() as session:
            return session.scalar(select(JobRecord.id).where(JobRecord.source == source, JobRecord.dedupe_key == dedupe_key).limit(1)) is not None

    def get_by_dedupe(self, source: str, dedupe_key: str) -> Job | None:
        with self.sessions() as session:
            record = session.scalar(select(JobRecord).where(JobRecord.source == source, JobRecord.dedupe_key == dedupe_key).limit(1))
            return Job.model_validate_json(record.payload) if record else None

    def save(self, job: Job, dedupe_key: str) -> Job:
        now = datetime.now(timezone.utc)
        job.updated_at = now
        with self.sessions.begin() as session:
            record = session.get(JobRecord, job.id)
            if record is None:
                record = JobRecord(id=job.id, source=job.source, source_job_id=job.source_job_id, dedupe_key=dedupe_key,
                                   status=job.status.value, final_score=job.final_score,
                                   payload=job.model_dump_json(), created_at=job.created_at, updated_at=now)
                session.add(record)
            else:
                record.status = job.status.value
                record.final_score = job.final_score
                record.payload = job.model_dump_json()
                record.updated_at = now
        return job

    def get(self, job_id: str) -> Job | None:
        with self.sessions() as session:
            record = session.get(JobRecord, job_id)
            return Job.model_validate_json(record.payload) if record else None

    def transition(self, job_id: str, target: JobStatus) -> Job:
        job = self.get(job_id)
        if job is None:
            raise KeyError(job_id)
        validate_transition(job.status, target)
        job.status = target
        return self.save(job, dedupe_key_for(job))

    def save_feedback(self, feedback: Feedback) -> None:
        if self.get(feedback.job_id) is None:
            raise KeyError(feedback.job_id)
        with self.sessions.begin() as session:
            session.add(FeedbackRecord(job_id=feedback.job_id, feedback=feedback.feedback.value, reason=feedback.reason))

    def reserve_notification(self, job_id: str, analyzer_version: str, channel: str = "telegram") -> bool:
        now = datetime.now(timezone.utc)
        try:
            with self.sessions.begin() as session:
                session.add(NotificationRecord(
                    job_id=job_id, channel=channel, analyzer_version=analyzer_version,
                    status="reserved", error=None, created_at=now, updated_at=now,
                ))
            return True
        except IntegrityError:
            return False

    def baseline_existing_notifications(
        self,
        analyzer_version: str,
        channel: str = "telegram",
    ) -> int:
        """Durably suppress notifications for every job present before live starts."""
        now = datetime.now(timezone.utc)
        with self.sessions.begin() as session:
            job_ids = set(session.scalars(select(JobRecord.id)).all())
            already_classified = set(session.scalars(select(NotificationRecord.job_id).where(
                NotificationRecord.channel == channel,
                NotificationRecord.analyzer_version == analyzer_version,
            )).all())
            baseline_ids = job_ids - already_classified
            session.add_all([
                NotificationRecord(
                    job_id=job_id,
                    channel=channel,
                    analyzer_version=analyzer_version,
                    status="suppressed_pre_live",
                    error=None,
                    created_at=now,
                    updated_at=now,
                )
                for job_id in baseline_ids
            ])
        return len(baseline_ids)

    def mark_notification(self, job_id: str, analyzer_version: str, status: str, error: str | None = None, channel: str = "telegram") -> None:
        with self.sessions.begin() as session:
            record = session.scalar(select(NotificationRecord).where(
                NotificationRecord.job_id == job_id,
                NotificationRecord.channel == channel,
                NotificationRecord.analyzer_version == analyzer_version,
            ))
            if record is None:
                raise KeyError((job_id, channel, analyzer_version))
            record.status = status
            record.error = error[:300] if error else None
            record.updated_at = datetime.now(timezone.utc)

    def notification_status(self, job_id: str, analyzer_version: str, channel: str = "telegram") -> str | None:
        with self.sessions() as session:
            return session.scalar(select(NotificationRecord.status).where(
                NotificationRecord.job_id == job_id,
                NotificationRecord.channel == channel,
                NotificationRecord.analyzer_version == analyzer_version,
            ))

    def register_existing_feed(self, source: str, feed_name: str, feed_url: str) -> bool:
        """Mark a previously live feed as onboarded without suppressing future identities."""
        with self.sessions.begin() as session:
            if session.get(RSSFeedStateRecord, feed_url) is not None:
                return False
            session.add(RSSFeedStateRecord(
                feed_url=feed_url,
                source=source,
                feed_name=feed_name,
                baseline_identity_count=0,
                onboarded_at=datetime.now(timezone.utc),
            ))
        return True

    def feed_is_onboarded(self, feed_url: str) -> bool:
        with self.sessions() as session:
            return session.get(RSSFeedStateRecord, feed_url) is not None

    def onboard_feed(
        self,
        source: str,
        feed_name: str,
        feed_url: str,
        source_identities: set[str],
    ) -> int:
        """Atomically persist the first observed identity set for a newly added feed."""
        identities = sorted(source_identities)
        now = datetime.now(timezone.utc)
        with self.sessions.begin() as session:
            if session.get(RSSFeedStateRecord, feed_url) is not None:
                return 0
            session.add(RSSFeedStateRecord(
                feed_url=feed_url,
                source=source,
                feed_name=feed_name,
                baseline_identity_count=len(identities),
                onboarded_at=now,
            ))
            session.add_all([
                RSSFeedBaselineIdentityRecord(
                    feed_url=feed_url,
                    source=source,
                    source_identity=identity,
                    created_at=now,
                )
                for identity in identities
            ])
        return len(identities)

    def is_suppressed_feed_identity(self, source: str, source_identity: str) -> bool:
        with self.sessions() as session:
            return session.scalar(select(RSSFeedBaselineIdentityRecord.id).where(
                RSSFeedBaselineIdentityRecord.source == source,
                RSSFeedBaselineIdentityRecord.source_identity == source_identity,
            ).limit(1)) is not None

    def feed_baseline_summary(self) -> dict[str, object]:
        with self.sessions() as session:
            feeds = session.scalars(select(RSSFeedStateRecord).order_by(RSSFeedStateRecord.onboarded_at)).all()
            identities = session.scalars(select(RSSFeedBaselineIdentityRecord.source_identity)).all()
            return {
                "onboarded_feeds": len(feeds),
                "baseline_identity_associations": len(identities),
                "baseline_unique_identities": len(set(identities)),
                "feeds": [
                    {
                        "feed_name": item.feed_name,
                        "feed_url": item.feed_url,
                        "baseline_identity_count": item.baseline_identity_count,
                        "onboarded_at": item.onboarded_at.isoformat(),
                    }
                    for item in feeds
                ],
            }

    def record_action(self, job_id: str, action: str, analyzer_version: str) -> bool:
        if self.get(job_id) is None:
            raise KeyError(job_id)
        try:
            with self.sessions.begin() as session:
                session.add(JobActionRecord(
                    job_id=job_id, action=action, analyzer_version=analyzer_version,
                    created_at=datetime.now(timezone.utc),
                ))
            return True
        except IntegrityError:
            return False

    def list_actions(self, job_id: str) -> list[dict[str, object]]:
        with self.sessions() as session:
            records = session.scalars(select(JobActionRecord).where(JobActionRecord.job_id == job_id).order_by(JobActionRecord.id)).all()
            return [{
                "action": item.action,
                "analyzer_version": item.analyzer_version,
                "created_at": item.created_at,
            } for item in records]

    def reserve_draft_request(self, job_id: str, analyzer_version: str) -> bool:
        now = datetime.now(timezone.utc)
        try:
            with self.sessions.begin() as session:
                session.add(DraftRequestRecord(
                    job_id=job_id, analyzer_version=analyzer_version, status="reserved",
                    draft=None, error=None, created_at=now, updated_at=now,
                ))
            return True
        except IntegrityError:
            return False

    def finish_draft_request(self, job_id: str, analyzer_version: str, draft: str | None = None, error: str | None = None) -> None:
        with self.sessions.begin() as session:
            record = session.scalar(select(DraftRequestRecord).where(
                DraftRequestRecord.job_id == job_id,
                DraftRequestRecord.analyzer_version == analyzer_version,
            ))
            if record is None:
                raise KeyError((job_id, analyzer_version))
            record.status = "completed" if draft is not None else "failed"
            record.draft = draft
            record.error = error[:300] if error else None
            record.updated_at = datetime.now(timezone.utc)

    def get_draft_request(self, job_id: str, analyzer_version: str) -> dict[str, object] | None:
        with self.sessions() as session:
            record = session.scalar(select(DraftRequestRecord).where(
                DraftRequestRecord.job_id == job_id,
                DraftRequestRecord.analyzer_version == analyzer_version,
            ))
            if record is None:
                return None
            return {"status": record.status, "draft": record.draft, "error": record.error, "updated_at": record.updated_at}

    def record_processing_error(self, stage: str, error_kind: str, message: str, job_id: str | None = None) -> None:
        with self.sessions.begin() as session:
            session.add(ProcessingErrorRecord(
                job_id=job_id, stage=stage, error_kind=error_kind,
                message=message[:300], created_at=datetime.now(timezone.utc),
            ))

    def list_processing_errors(self) -> list[dict[str, object]]:
        with self.sessions() as session:
            records = session.scalars(select(ProcessingErrorRecord).order_by(ProcessingErrorRecord.id)).all()
            return [{
                "job_id": item.job_id, "stage": item.stage, "error_kind": item.error_kind,
                "message": item.message, "created_at": item.created_at,
            } for item in records]

    def list_shortlisted(self) -> list[Job]:
        with self.sessions() as session:
            records = session.scalars(select(JobRecord).where(JobRecord.status.in_([JobStatus.SHORTLISTED.value, JobStatus.RESPONSE_PREPARED.value])).order_by(JobRecord.final_score.desc())).all()
            return [Job.model_validate_json(record.payload) for record in records]

    def list_all(self) -> list[Job]:
        with self.sessions() as session:
            records = session.scalars(select(JobRecord).order_by(JobRecord.created_at.asc())).all()
            return [Job.model_validate_json(record.payload) for record in records]


def dedupe_key_for(job: Job) -> str:
    import hashlib
    if job.source_job_id:
        return f"id:{job.source_job_id}"
    canonical = "|".join((job.url or "", job.title.strip().lower(), job.description.strip().lower()[:500]))
    return "hash:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
