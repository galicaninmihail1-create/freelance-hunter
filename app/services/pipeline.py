from __future__ import annotations

import logging

from app.ai import AIProvider
from app.db import JobRepository, dedupe_key_for
from app.filters import HardFilter
from app.models import Job, JobStatus, RawJob
from app.normalizer import normalize
from app.scoring import ScoringEngine
from app.telegram import TelegramClient

logger = logging.getLogger(__name__)


class JobPipeline:
    def __init__(self, repository: JobRepository, hard_filter: HardFilter, ai: AIProvider, scoring: ScoringEngine, telegram: TelegramClient, notification_threshold: float):
        self.repository, self.hard_filter, self.ai, self.scoring, self.telegram = repository, hard_filter, ai, scoring, telegram
        self.notification_threshold = notification_threshold

    async def process(self, raw: RawJob) -> Job | None:
        job = normalize(raw)
        key = dedupe_key_for(job)
        if self.repository.exists(job.source, key):
            logger.info("Skipping duplicate job source=%s key=%s", job.source, key[:20])
            return None
        self.repository.save(job, key)
        passed = self.hard_filter.evaluate(job)
        if not passed.accepted:
            job.status = JobStatus.REJECTED
            job.commercial_risks = [passed.reason or "hard_filter"]
            return self.repository.save(job, key)
        try:
            analysis = await self.ai.analyze_job(job)
            job = self.scoring.analyze(job, analysis)
            job.status = JobStatus.ANALYZED
            if (job.final_score or 0) < self.notification_threshold:
                job.status = JobStatus.REJECTED
                return self.repository.save(job, key)
            draft = await self.ai.generate_response(job, analysis)
            job.response_draft = draft.text
            job.status = JobStatus.RESPONSE_PREPARED
            self.repository.save(job, key)
            await self.telegram.notify(job, self.scoring)
            return self.repository.save(job, key)
        except Exception:
            logger.exception("Analysis failed for job %s; job remains persisted", job.id)
            return job

    async def process_many(self, raw_jobs: list[RawJob]) -> list[Job]:
        processed: list[Job] = []
        for raw in raw_jobs:
            try:
                job = await self.process(raw)
                if job:
                    processed.append(job)
            except Exception:
                logger.exception("Unhandled pipeline failure for an individual job")
        return processed
