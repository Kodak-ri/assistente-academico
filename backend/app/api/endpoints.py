import logging
from datetime import datetime, date, time, timedelta, timezone, tzinfo
from typing import Optional, List
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from backend.app.core.config import settings
from backend.app.core.database import get_db
from backend.app.models.db_models import Task, Course, Material, CalendarAllocation, DomesticRoutine
from backend.app.models.schemas import (
    TaskCompleteResponse,
    TaskSnoozeResponse,
    AllocationSummary,
    RoutineCompleteResponse,
    DailyDashboardResponse,
    DashboardAllocationItem,
    DashboardReadingItem,
    DashboardRoutineItem,
)
from backend.app.services.calendar_sync import sync_task_to_google_calendar
from backend.app.services.scheduler_engine import (
    InsufficientScheduleWindowError,
    ScheduleCapacityExceededError,
)

logger = logging.getLogger("academic_platform.api")

router = APIRouter(prefix="/api", tags=["Academic Assistant Core"])


@router.post(
    "/tasks/{task_id}/complete",
    response_model=TaskCompleteResponse,
    summary="Marcar tarefa como concluída",
)
def complete_task(
    task_id: str,
    db: Session = Depends(get_db),
) -> TaskCompleteResponse:
    """Callback acionado pelo botão 'Concluir' do ntfy ou WebUI.
    
    Transiciona o estado da tarefa para 'COMPLETED' de forma atômica e transacional.
    """
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Tarefa '{task_id}' não encontrada.",
        )

    task.status = "COMPLETED"
    db.commit()
    db.refresh(task)

    logger.info("Tarefa '%s' marcada como COMPLETED com sucesso.", task_id)
    return TaskCompleteResponse(
        status="success",
        message=f"Tarefa '{task_id}' concluída com sucesso.",
        task_id=task.id,
        task_status=task.status,
    )


@router.post(
    "/tasks/{task_id}/snooze",
    response_model=TaskSnoozeResponse,
    summary="Adiar blocos da tarefa (Snooze)",
)
def snooze_task(
    task_id: str,
    min: int = Query(default=60, gt=0, description="Minutos a adiar a partir de agora"),
    db: Session = Depends(get_db),
) -> TaskSnoozeResponse:
    """Aciona o recálculo do agendamento a partir de now + timedelta(minutes=min).
    
    Recalcula o agendamento linear determinístico, atualiza os eventos no Google Calendar
    e ajusta a tabela de alocações mantendo integridade transacional.
    """
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Tarefa '{task_id}' não encontrada.",
        )

    # Nova fronteira temporal de início
    new_now = datetime.now(timezone.utc) + timedelta(minutes=min)

    # Obter o serviço do Google Calendar se as credenciais existirem
    calendar_service = None
    try:
        from backend.app.services.google_client import get_calendar_service
        calendar_service = get_calendar_service()
    except Exception as exc:
        logger.debug("Serviço do Google Calendar offline ou não autenticado: %s", exc)
        calendar_service = None

    try:
        new_allocations = sync_task_to_google_calendar(
            db_session=db,
            task_id=task_id,
            calendar_service=calendar_service,
            now=new_now,
            force_reallocate=True,
        )
    except InsufficientScheduleWindowError as exc:
        db.rollback()
        logger.warning("Falha ao adiar tarefa '%s' por janela insuficiente: %s", task_id, exc)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Não há janela de agendamento suficiente para adiar a tarefa respeitando a margem antivéspera de 24h: {exc}",
        )
    except ScheduleCapacityExceededError as exc:
        db.rollback()
        logger.warning("Falha ao adiar tarefa '%s' por capacidade excedida: %s", task_id, exc)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Capacidade de agenda insuficiente para alocar os blocos da tarefa: {exc}",
        )
    except Exception as exc:
        db.rollback()
        logger.error("Erro inesperado ou conectividade ao adiar tarefa '%s': %s", task_id, exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Falha de serviço ou conectividade ao recalcular agendamento: {exc}",
        )

    summary_list = [
        AllocationSummary(
            id=alloc.id,
            start_time=alloc.start_time.isoformat(),
            end_time=alloc.end_time.isoformat(),
            gcal_event_id=alloc.gcal_event_id,
        )
        for alloc in new_allocations
    ]

    logger.info("Tarefa '%s' adiada por %d minutos. %d novos blocos gerados.", task_id, min, len(new_allocations))
    return TaskSnoozeResponse(
        status="success",
        message=f"Tarefa '{task_id}' adiada com sucesso por {min} minutos.",
        task_id=task.id,
        new_allocations_count=len(summary_list),
        allocations=summary_list,
    )


@router.post(
    "/routines/{routine_id}/complete",
    response_model=RoutineCompleteResponse,
    summary="Marcar rotina residencial como concluída",
)
def complete_routine(
    routine_id: int,
    db: Session = Depends(get_db),
) -> RoutineCompleteResponse:
    """Callback acionado pelo botão 'Concluir' na notificação de rotina doméstica.
    
    Atualiza domestic_routines.last_completed com o timestamp atual (UTC).
    """
    routine = db.query(DomesticRoutine).filter(DomesticRoutine.id == routine_id).first()
    if not routine:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Rotina residencial {routine_id} não encontrada.",
        )

    now_utc = datetime.now(timezone.utc)
    routine.last_completed = now_utc
    db.commit()
    db.refresh(routine)

    logger.info("Rotina residencial '%s' (ID %d) concluída em %s.", routine.name, routine.id, now_utc.isoformat())
    return RoutineCompleteResponse(
        status="success",
        message=f"Rotina '{routine.name}' concluída com sucesso.",
        routine_id=routine.id,
        last_completed=now_utc.isoformat(),
    )


@router.get(
    "/dashboard/daily",
    response_model=DailyDashboardResponse,
    summary="Painel consolidado da jornada diária",
)
def get_daily_dashboard(
    date_str: Optional[str] = Query(default=None, alias="date", description="Data no formato YYYY-MM-DD"),
    db: Session = Depends(get_db),
) -> DailyDashboardResponse:
    """Endpoint consolidado que retorna a situação completa da jornada diária.
    
    Retorna os blocos de estudo confirmados para o dia, a fila de leitura pendente
    e as rotinas residenciais com respectivo estado de conclusão no ciclo atual.
    """
    app_tz: tzinfo
    try:
        app_tz = ZoneInfo(settings.TIMEZONE)
    except Exception:
        app_tz = timezone.utc

    if date_str:
        try:
            target_date = date.fromisoformat(date_str)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Formato de data inválido. Utilize o padrão ISO 'YYYY-MM-DD'.",
            )
    else:
        target_date = datetime.now(app_tz).date()

    day_start = datetime.combine(target_date, time.min)
    day_end = datetime.combine(target_date, time.max)

    # 1. Alocações e blocos do dia
    allocations_query = (
        db.query(CalendarAllocation, Task, Course)
        .join(Task, CalendarAllocation.task_id == Task.id)
        .outerjoin(Course, Task.course_id == Course.id)
        .filter(CalendarAllocation.start_time >= day_start, CalendarAllocation.start_time <= day_end)
        .order_by(CalendarAllocation.start_time.asc())
        .all()
    )

    classes_and_allocations: List[DashboardAllocationItem] = []
    for alloc, t, c in allocations_query:
        classes_and_allocations.append(
            DashboardAllocationItem(
                allocation_id=alloc.id,
                task_id=t.id,
                task_title=t.title,
                course_id=c.id if c else t.course_id,
                course_name=c.name if c else "Sem Disciplina",
                start_time=alloc.start_time.isoformat(),
                end_time=alloc.end_time.isoformat(),
                gcal_event_id=alloc.gcal_event_id,
                status=t.status,
            )
        )

    # 2. Fila de leitura ordenada (tarefas PENDING e SCHEDULED)
    pending_tasks_query = (
        db.query(Task, Course, Material)
        .outerjoin(Course, Task.course_id == Course.id)
        .outerjoin(Material, Material.course_id == Task.course_id)
        .filter(Task.status.in_(["PENDING", "SCHEDULED"]))
        .order_by(Task.due_datetime.asc())
        .all()
    )

    reading_queue: List[DashboardReadingItem] = []
    for t, c, m in pending_tasks_query:
        reading_queue.append(
            DashboardReadingItem(
                task_id=t.id,
                title=t.title,
                course_id=c.id if c else t.course_id,
                course_name=c.name if c else "Sem Disciplina",
                due_datetime=t.due_datetime.isoformat(),
                est_duration_min=t.est_duration_min,
                page_count=m.page_count if m else 0,
                status=t.status,
                group_required=t.group_required,
            )
        )

    # 3. Rotinas residenciais
    routines = db.query(DomesticRoutine).all()
    domestic_routines_list: List[DashboardRoutineItem] = []
    for r in routines:
        is_completed_today = False
        if r.last_completed:
            # Verifica se last_completed foi na mesma data
            completed_dt = r.last_completed
            if completed_dt.tzinfo is None:
                completed_dt = completed_dt.replace(tzinfo=timezone.utc)
            completed_date = completed_dt.astimezone(app_tz).date()
            if completed_date == target_date:
                is_completed_today = True

        domestic_routines_list.append(
            DashboardRoutineItem(
                id=r.id,
                name=r.name,
                cron_expression=r.cron_expression,
                last_completed=r.last_completed.isoformat() if r.last_completed else None,
                is_completed_today=is_completed_today,
            )
        )

    return DailyDashboardResponse(
        date=target_date.isoformat(),
        classes_and_allocations=classes_and_allocations,
        reading_queue=reading_queue,
        domestic_routines=domestic_routines_list,
    )
