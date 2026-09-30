"""
Autonomous Procurement Agent -- scheduled background tasks.
Runs inside FastAPI process via APScheduler.
"""
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

async def recover_stuck_offers():
    """Reset offers stuck in 'analyzing' state for >45 minutes back to 'uploaded'."""
    from database import AsyncSessionLocal
    from models import Offer, OfferStatus
    from sqlalchemy import select
    cutoff = datetime.utcnow() - timedelta(minutes=45)
    async with AsyncSessionLocal() as db:
        try:
            result = await db.execute(
                select(Offer).where(
                    Offer.status == OfferStatus.analyzing,
                    Offer.created_at < cutoff
                )
            )
            stuck = result.scalars().all()
            if stuck:
                for offer in stuck:
                    offer.status = OfferStatus.uploaded
                    logger.warning(f"Recovered stuck offer {offer.id}")
                await db.commit()
                logger.info(f"Recovered {len(stuck)} stuck offer(s)")
        except Exception as e:
            logger.error(f"Recover stuck offers failed: {e}")

async def daily_savings_summary():
    """Compute daily savings summary across all audits and log it."""
    from database import AsyncSessionLocal
    from models import Audit, Offer, OfferStatus
    from sqlalchemy import select, func
    async with AsyncSessionLocal() as db:
        try:
            today = datetime.utcnow().date()
            result = await db.execute(
                select(func.sum(Audit.total_savings_identified))
                .where(func.date(Audit.created_at) == today)
            )
            daily_savings = result.scalar() or 0
            result2 = await db.execute(
                select(func.count(Offer.id))
                .where(Offer.status == OfferStatus.analyzed)
            )
            total_analyzed = result2.scalar() or 0
            logger.info(
                f"[Daily Summary] Analyzed offers total: {total_analyzed} | "
                f"Savings identified today: EUR{daily_savings:,.2f}"
            )
            import os
            admin_email = os.getenv("ADMIN_EMAIL", "")
            smtp_host = os.getenv("SMTP_HOST", "")
            if admin_email and smtp_host:
                from services.email_sender import send_negotiation_email
                send_negotiation_email(
                    to_email=admin_email,
                    subject=f"NegotiateX Daily Report -- {today}",
                    body=f"Tagesreport {today}\n\nAnalysierte Angebote gesamt: {total_analyzed}\nIdentifizierte Einsparungen heute: EUR{daily_savings:,.2f}\n\n-- NegotiateX Autonomous Agent"
                )
        except Exception as e:
            logger.error(f"Daily summary failed: {e}")

async def update_benchmark_stats():
    """Refresh aggregated benchmark statistics in the database."""
    from database import AsyncSessionLocal
    from models import BenchmarkEntry
    from sqlalchemy import select, func
    async with AsyncSessionLocal() as db:
        try:
            result = await db.execute(
                select(
                    BenchmarkEntry.category,
                    func.count(BenchmarkEntry.id).label("count"),
                    func.avg(BenchmarkEntry.value).label("avg_value"),
                    func.min(BenchmarkEntry.value).label("min_value"),
                    func.max(BenchmarkEntry.value).label("max_value"),
                ).group_by(BenchmarkEntry.category)
            )
            stats = result.all()
            if stats:
                logger.info(f"[Benchmark Stats] {len(stats)} categories updated")
                for s in stats:
                    logger.debug(f"  {s.category}: n={s.count}, avg=EUR{s.avg_value:.2f}")
        except Exception as e:
            logger.error(f"Benchmark stats update failed: {e}")

def start_scheduler():
    """Initialize and start the APScheduler background scheduler."""
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger
    scheduler = AsyncIOScheduler(timezone="Europe/Berlin")
    scheduler.add_job(
        recover_stuck_offers,
        trigger=IntervalTrigger(minutes=30),
        id="recover_stuck",
        name="Recover stuck analyses",
        replace_existing=True,
    )
    scheduler.add_job(
        daily_savings_summary,
        trigger=CronTrigger(hour=8, minute=0),
        id="daily_summary",
        name="Daily savings summary",
        replace_existing=True,
    )
    scheduler.add_job(
        update_benchmark_stats,
        trigger=IntervalTrigger(hours=1),
        id="benchmark_stats",
        name="Update benchmark stats",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("Autonomous Procurement Agent started (3 scheduled tasks)")
    return scheduler
