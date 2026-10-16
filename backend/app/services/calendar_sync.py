import math
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Tuple

from sqlalchemy.orm import Session

from backend.app.models.db_models import Task, Material, CalendarAllocation
from backend.app.services.google_client import parse_rfc3339_datetime
from backend.app.services.scheduler_engine import (
    DeterministicSchedulerEngine,
    SchedulePlan,
    ScheduledBlock,
)

logger = logging.getLogger("academic_platform.calendar_sync")


def fetch_freebusy_intervals(
    calendar_service,
    start_dt: datetime,
    end_dt: datetime,
    calendar_id: str = "primary",
) -> List[Tuple[datetime, datetime]]:
    """Consulta os intervalos de ocupação no Google Calendar via freebusy.query."""
    if not calendar_service:
        return []

    # Normalização em ISO 8601 RFC3339
    time_min = start_dt.astimezone(timezone.utc).isoformat()
    time_max = end_dt.astimezone(timezone.utc).isoformat()

    body = {
        "timeMin": time_min,
        "timeMax": time_max,
        "items": [{"id": calendar_id}],
    }

    response = calendar_service.freebusy().query(body=body).execute()
    calendars = response.get("calendars", {})
    primary_cal = calendars.get(calendar_id, {})
    busy_list = primary_cal.get("busy", [])

    busy_intervals: List[Tuple[datetime, datetime]] = []
    for item in busy_list:
        s_dt = parse_rfc3339_datetime(item.get("start"))
        e_dt = parse_rfc3339_datetime(item.get("end"))
        if s_dt and e_dt:
            busy_intervals.append((s_dt, e_dt))

    return busy_intervals


def create_gcal_study_event(
    calendar_service,
    task_title: str,
    block: ScheduledBlock,
    material: Optional[Material] = None,
    calendar_id: str = "primary",
) -> str:
    """Inscreve um evento de estudo no Google Calendar com metadados e metas de leitura."""
    summary = f"[Estudo] {task_title} (Bloco {block.block_index}/{block.total_blocks})"

    description_parts = [
        f"Sessão planejada pelo Assistente Acadêmico Autônomo.",
        f"Atividade: {task_title}",
        f"Duração: {block.duration_min} minutos (com 10 min de buffer pré e pós).",
    ]

    if material:
        if material.page_count > 0:
            # Meta proporcional por bloco
            pages_per_block = math.ceil(material.page_count / float(block.total_blocks))
            # Meta acumulada da sessão
            target_start_page = (block.block_index - 1) * pages_per_block + 1
            target_end_page = min(material.page_count, block.block_index * pages_per_block)
            description_parts.append(
                f"Meta: {pages_per_block} páginas (Páginas {target_start_page} a {target_end_page} de {material.page_count})"
            )
        if material.id:
            drive_url = f"https://drive.google.com/file/d/{material.id}/view"
            description_parts.append(f"Material no Google Drive: {drive_url}")

    event_body = {
        "summary": summary,
        "description": "\n".join(description_parts),
        "start": {
            "dateTime": block.start_time.astimezone(timezone.utc).isoformat(),
        },
        "end": {
            "dateTime": block.end_time.astimezone(timezone.utc).isoformat(),
        },
        "reminders": {
            "useDefault": False,
            "overrides": [
                {"method": "popup", "minutes": 10},
            ],
        },
    }

    created_event = calendar_service.events().insert(
        calendarId=calendar_id,
        body=event_body,
    ).execute()

    return created_event.get("id", f"mock-event-{block.task_id}-{block.block_index}")


def sync_task_to_google_calendar(
    db_session: Session,
    task_id: str,
    calendar_service=None,
    now: Optional[datetime] = None,
    calendar_id: str = "primary",
    force_reallocate: bool = False,
) -> List[CalendarAllocation]:
    """Fluxo unificado da SPEC-002:
    1. Busca a tarefa e material no banco.
    2. Garante idempotência: se a tarefa já estiver SCHEDULED com alocações ativas, retorna sem duplicar.
    3. Consulta freebusy no Google Calendar para coletar intervalos ocupados.
    4. Executa o scheduler_engine linear determinístico.
    5. Inscreve os eventos criados no Google Calendar.
    6. Persiste as alocações em calendar_allocations e atualiza task.status para 'SCHEDULED'.
    """
    task = db_session.query(Task).filter(Task.id == task_id).first()
    if not task:
        raise ValueError(f"Tarefa '{task_id}' não encontrada na base de dados.")

    # -------------------------------------------------------------------------
    # Idempotência: verifica se a tarefa já possui alocações
    # -------------------------------------------------------------------------
    existing_allocations = db_session.query(CalendarAllocation).filter(
        CalendarAllocation.task_id == task_id
    ).all()

    if existing_allocations and not force_reallocate:
        logger.info(f"Tarefa '{task_id}' já possui {len(existing_allocations)} alocações. Idempotência preservada.")
        return existing_allocations

    if existing_allocations and force_reallocate:
        # Remove alocações anteriores em caso de realocação forçada
        for alloc in existing_allocations:
            db_session.delete(alloc)
        db_session.commit()

    # -------------------------------------------------------------------------
    # Consulta de Ocupação (Freebusy)
    # -------------------------------------------------------------------------
    ref_now = now or datetime.now(timezone.utc)
    if ref_now.tzinfo is None:
        ref_now = ref_now.replace(tzinfo=timezone.utc)

    target_deadline = task.due_datetime
    if target_deadline.tzinfo is None:
        target_deadline = target_deadline.replace(tzinfo=timezone.utc)

    busy_intervals: List[Tuple[datetime, datetime]] = []
    if calendar_service:
        t_alvo_fim = target_deadline - timedelta(hours=24)
        if ref_now < t_alvo_fim:
            busy_intervals = fetch_freebusy_intervals(
                calendar_service=calendar_service,
                start_dt=ref_now,
                end_dt=t_alvo_fim,
                calendar_id=calendar_id,
            )

    # -------------------------------------------------------------------------
    # Execução do Motor Determinístico de Agendamento
    # -------------------------------------------------------------------------
    engine = DeterministicSchedulerEngine(buffer_min=10)
    plan: SchedulePlan = engine.compute_schedule(
        task_id=task.id,
        est_duration_min=task.est_duration_min,
        due_datetime=target_deadline,
        now=ref_now,
        busy_intervals=busy_intervals,
    )

    # Busca material associado para enriquecer o evento
    material = db_session.query(Material).filter(Material.course_id == task.course_id).first()

    # -------------------------------------------------------------------------
    # Inscrição de Eventos no Google Calendar & Persistência Atômica no SQLite
    # -------------------------------------------------------------------------
    created_allocations: List[CalendarAllocation] = []

    for block in plan.blocks:
        if calendar_service:
            gcal_id = create_gcal_study_event(
                calendar_service=calendar_service,
                task_title=task.title,
                block=block,
                material=material,
                calendar_id=calendar_id,
            )
        else:
            # Fallback seguro para simulação/testes offline
            gcal_id = f"gcal-event-{task.id}-blk{block.block_index}"

        allocation = CalendarAllocation(
            task_id=task.id,
            gcal_event_id=gcal_id,
            start_time=block.start_time,
            end_time=block.end_time,
        )
        db_session.add(allocation)
        created_allocations.append(allocation)

    # Atualiza o status da tarefa para SCHEDULED
    task.status = "SCHEDULED"
    db_session.commit()

    return created_allocations
