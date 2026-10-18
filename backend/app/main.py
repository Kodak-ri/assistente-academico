import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, date, time, timedelta, timezone, tzinfo
from typing import Optional, List, Dict, Any, AsyncGenerator
from zoneinfo import ZoneInfo

from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

from backend.app.api.endpoints import router as api_router
from backend.app.core.config import settings
from backend.app.core.database import SessionLocal, init_db
from backend.app.models.db_models import Task, Course, Material, CalendarAllocation, DomesticRoutine
from backend.app.services.calendar_sync import sync_task_to_google_calendar
from backend.app.services.notifier import NtfyNotifier, default_notifier

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("academic_platform.main")


async def run_periodic_scan(
    notifier: Optional[NtfyNotifier] = None,
    db_session: Optional[Session] = None,
    workspace_client: Any = None,
    gemini_client: Any = None,
    reference_now: Optional[datetime] = None,
) -> None:
    """Orquestra o pipeline periódico completo de sincronização e agendamento:
    
    1. Sincroniza turmas e tarefas ativas do Google Classroom via GoogleWorkspaceClient.
    2. Analisa novos PDFs no Gemini (disparando alerta urgente se group_required=True).
    3. Agenda tarefas PENDING no Google Calendar com o motor determinístico.
    """
    notif = notifier or default_notifier
    logger.info("Iniciando varredura periódica de sincronização e agendamento.")

    owns_session = db_session is None
    db = db_session if db_session is not None else SessionLocal()

    try:
        ws_client = workspace_client
        if ws_client is None:
            try:
                from backend.app.services.google_client import get_google_workspace_client
                ws_client = get_google_workspace_client()
            except Exception as exc:
                logger.debug("Google Workspace client offline: %s", exc)

        if ws_client and ws_client.classroom_service:
            try:
                active_courses = ws_client.filter_active_courses(
                    db_session=db,
                    reference_date=reference_now,
                )
                for course_info in active_courses:
                    c_id = str(course_info.get("id"))
                    pdf_items = ws_client.extract_pdf_attachments(c_id)
                    for item in pdf_items:
                        t_id = str(item.get("work_id"))
                        existing_task = db.query(Task).filter(Task.id == t_id).first()
                        if not existing_task:
                            try:
                                ref_dt = reference_now or datetime.now(timezone.utc)
                                due = item.get("due_datetime") or (ref_dt + timedelta(days=7))
                                pdf_bytes = ws_client.download_drive_pdf_to_memory(item["file_id"])
                                from backend.app.services.document_analyzer import process_document_and_persist
                                effort = process_document_and_persist(
                                    pdf_source=pdf_bytes,
                                    course_id=c_id,
                                    course_name=course_info.get("name", "Disciplina"),
                                    task_id=t_id,
                                    task_title=item.get("work_title", "Atividade"),
                                    due_datetime=due,
                                    file_id=item["file_id"],
                                    db_session=db,
                                    gemini_client=gemini_client,
                                )
                                if effort.group_required:
                                    await notif.send_group_urgent_alert(
                                        task_id=t_id,
                                        task_title=item.get("work_title", "Atividade"),
                                        course_name=course_info.get("name", "Disciplina"),
                                        due_datetime=due,
                                        reasoning=effort.reasoning_summary,
                                    )
                                # Pequena pausa assíncrona para respeitar a taxa de requisições do Gemini
                                await asyncio.sleep(1.5)
                            except Exception as item_exc:
                                logger.warning("Falha ao analisar anexo '%s' da turma %s: %s", item.get("work_title"), c_id, item_exc)
                                continue
            except Exception as exc:
                logger.warning("Erro no processamento periódico do Classroom/Gemini: %s", exc)

        # Agendamento de tarefas PENDING no Google Calendar
        cal_service = ws_client.calendar_service if ws_client else None
        pending_tasks = db.query(Task).filter(Task.status == "PENDING").all()
        for t in pending_tasks:
            try:
                sync_task_to_google_calendar(
                    db_session=db,
                    task_id=t.id,
                    calendar_service=cal_service,
                    now=reference_now,
                    force_reallocate=False,
                )
                logger.info("Tarefa '%s' agendada com sucesso no pipeline periódico.", t.id)
            except Exception as exc:
                logger.warning("Não foi possível agendar tarefa '%s' no ciclo atual: %s", t.id, exc)
    finally:
        if owns_session:
            db.close()

    logger.info("Varredura periódica concluída com sucesso.")


async def run_daily_briefing(
    notifier: Optional[NtfyNotifier] = None,
    db_session: Optional[Session] = None,
    reference_date: Optional[date] = None,
) -> None:
    """Dispara o despacho matinal diário das 07:30."""
    notif = notifier or default_notifier
    app_tz: tzinfo
    try:
        app_tz = ZoneInfo(settings.TIMEZONE)
    except Exception:
        app_tz = timezone.utc

    today_date = reference_date or datetime.now(app_tz).date()
    day_start = datetime.combine(today_date, time.min)
    day_end = datetime.combine(today_date, time.max)

    logger.info("Preparando despacho diário das 07:30 para a data %s.", today_date.isoformat())

    owns_session = db_session is None
    db = db_session if db_session is not None else SessionLocal()

    try:
        # Alocações do dia
        allocations_query = (
            db.query(CalendarAllocation, Task, Course)
            .join(Task, CalendarAllocation.task_id == Task.id)
            .outerjoin(Course, Task.course_id == Course.id)
            .filter(CalendarAllocation.start_time >= day_start, CalendarAllocation.start_time <= day_end)
            .order_by(CalendarAllocation.start_time.asc())
            .all()
        )
        study_blocks: List[Dict[str, Any]] = [
            {
                "task_title": t.title,
                "course_name": c.name if c else "Sem Disciplina",
                "start_time": alloc.start_time.strftime("%H:%M"),
                "end_time": alloc.end_time.strftime("%H:%M"),
            }
            for alloc, t, c in allocations_query
        ]

        # Fila de leituras pendentes (sem produto cartesiano com múltiplos materiais)
        pending_tasks = (
            db.query(Task)
            .filter(Task.status.in_(["PENDING", "SCHEDULED"]))
            .order_by(Task.due_datetime.asc())
            .all()
        )
        reading_queue: List[Dict[str, Any]] = []
        for t in pending_tasks:
            m = (
                db.query(Material)
                .filter(Material.course_id == t.course_id, Material.title == t.title)
                .first()
                or db.query(Material)
                .filter(Material.course_id == t.course_id)
                .first()
            )
            reading_queue.append(
                {
                    "title": t.title,
                    "page_count": m.page_count if m else 0,
                }
            )

        # Rotinas domésticas
        routines = db.query(DomesticRoutine).all()
        domestic_routines: List[Dict[str, Any]] = []
        for r in routines:
            is_completed = False
            if r.last_completed:
                c_dt = r.last_completed
                if c_dt.tzinfo is None:
                    c_dt = c_dt.replace(tzinfo=timezone.utc)
                is_completed = (c_dt.astimezone(app_tz).date() == today_date)

            domestic_routines.append({
                "name": r.name,
                "is_completed_today": is_completed,
            })
    finally:
        if owns_session:
            db.close()

    await notif.send_daily_briefing(
        date_str=today_date.strftime("%d/%m/%Y"),
        study_blocks=study_blocks,
        reading_queue=reading_queue,
        domestic_routines=domestic_routines,
    )
    logger.info("Despacho diário das 07:30 enviado com sucesso.")


async def run_domestic_routine_alert(routine_id: int, routine_name: str, notifier: Optional[NtfyNotifier] = None) -> None:
    """Dispara a notificação interativa de uma rotina residencial."""
    notif = notifier or default_notifier
    logger.info("Disparando alerta de rotina residencial '%s' (ID %d).", routine_name, routine_id)
    await notif.send_domestic_routine_alert(routine_id=routine_id, routine_name=routine_name)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Gerenciador de Ciclo de Vida nativo do FastAPI:
    
    Startup:
    - Inicializa tabelas do banco de dados SQLite.
    - Inicializa o agendador assíncrono APScheduler.
    - Registra a varredura periódica a cada 2 horas.
    - Registra o despacho matinal diário às 07:30.
    - Registra os triggers crontab para todas as rotinas em domestic_routines.
    
    Shutdown:
    - Drenagem e desligamento seguro do agendador sem perda de estado.
    """
    logger.info("Iniciando microsserviço Assistente Acadêmico Autônomo...")
    # 1. Banco de dados
    init_db()

    # 2. Agendador APScheduler
    app_tz: tzinfo
    try:
        app_tz = ZoneInfo(settings.TIMEZONE)
    except Exception:
        app_tz = timezone.utc

    scheduler = AsyncIOScheduler(timezone=app_tz)

    # Job: Varredura Periódica a cada 2 horas
    scheduler.add_job(
        run_periodic_scan,
        trigger=IntervalTrigger(hours=2),
        id="periodic_scan",
        name="Varredura periódica do Classroom e agendamento",
        replace_existing=True,
    )

    # Job: Despacho Matinal Diário às 07:30
    scheduler.add_job(
        run_daily_briefing,
        trigger=CronTrigger(hour=7, minute=30, timezone=app_tz),
        id="daily_briefing",
        name="Despacho diário matinal das 07:30",
        replace_existing=True,
    )

    # Carrega rotinas domésticas e registra seus respectivos crons
    with SessionLocal() as db:
        routines = db.query(DomesticRoutine).all()
        for r in routines:
            try:
                cron_trig = CronTrigger.from_crontab(r.cron_expression, timezone=app_tz)
                scheduler.add_job(
                    run_domestic_routine_alert,
                    trigger=cron_trig,
                    id=f"routine_{r.id}",
                    name=f"Rotina Doméstica: {r.name}",
                    args=[r.id, r.name],
                    replace_existing=True,
                )
                logger.info("Agendada rotina residencial '%s' com cron '%s'.", r.name, r.cron_expression)
            except Exception as exc:
                logger.warning("Falha ao registrar cron para rotina '%s' (%s): %s", r.name, r.cron_expression, exc)

    scheduler.start()
    app.state.scheduler = scheduler
    logger.info("APScheduler iniciado com sucesso. Total de jobs: %d", len(scheduler.get_jobs()))

    try:
        yield
    finally:
        logger.info("Encerrando microsserviço e drenando jobs do agendador...")
        if scheduler.running:
            scheduler.shutdown(wait=True)
            await asyncio.sleep(0.01)
        logger.info("APScheduler finalizado com sucesso.")


app = FastAPI(
    title="Assistente Acadêmico Autônomo",
    description="Núcleo FastAPI, Gestão de Ciclo de Vida (APScheduler) e Mensageria Interativa ntfy (SPEC-003)",
    version="1.0.0",
    lifespan=lifespan,
)

# Injeção das rotas REST
app.include_router(api_router)

STATIC_DIR = Path(__file__).resolve().parent / "static"

# Montagem dos arquivos estáticos PWA
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", summary="Interface Web PWA", response_class=FileResponse)
async def serve_index() -> FileResponse:
    """Serve o App Shell do Progressive Web App."""
    return FileResponse(str(STATIC_DIR / "index.html"), media_type="text/html")


@app.get("/manifest.json", summary="Manifesto PWA", response_class=FileResponse)
async def serve_manifest() -> FileResponse:
    """Serve o arquivo de manifesto W3C do PWA."""
    return FileResponse(str(STATIC_DIR / "manifest.json"), media_type="application/manifest+json")


@app.get("/service-worker.js", summary="Service Worker PWA", response_class=FileResponse)
async def serve_service_worker() -> FileResponse:
    """Serve o Service Worker com escopo raiz permitido."""
    return FileResponse(
        str(STATIC_DIR / "service-worker.js"),
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/"},
    )


@app.get("/health", summary="Verificação de integridade do microsserviço")
async def health_check() -> Dict[str, Any]:
    """Retorna o estado de saúde da aplicação e do agendador assíncrono."""
    scheduler: Optional[AsyncIOScheduler] = getattr(app.state, "scheduler", None)
    is_running = scheduler.running if scheduler else False
    jobs_list = [
        {"id": job.id, "name": job.name, "next_run_time": str(job.next_run_time)}
        for job in (scheduler.get_jobs() if scheduler else [])
    ]

    return {
        "status": "healthy",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "scheduler_running": is_running,
        "jobs_count": len(jobs_list),
        "jobs": jobs_list,
    }
