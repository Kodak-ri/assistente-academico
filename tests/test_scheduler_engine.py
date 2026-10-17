from datetime import datetime, date, time, timedelta, timezone
from unittest.mock import MagicMock
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.core.database import init_db
from backend.app.models.db_models import Course, Material, Task, CalendarAllocation
from backend.app.services.scheduler_engine import (
    DeterministicSchedulerEngine,
    InsufficientScheduleWindowError,
    ScheduleCapacityExceededError,
    calculate_cognitive_chunks,
    merge_busy_intervals,
    subtract_busy_from_window,
    get_daily_allowed_windows,
)
from backend.app.services.calendar_sync import sync_task_to_google_calendar


@pytest.fixture
def memory_db():
    """Cria um banco SQLite em memória isolado para os testes do scheduler."""
    engine = create_engine("sqlite:///:memory:", future=True)
    init_db(target_engine=engine)
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()


def test_cognitive_chunking_math():
    """Valida o cálculo matemático exato de fracionamento cognitivo anti-estafa:
    - est_duration_min <= 90: bloco único.
    - est_duration_min > 90: fracionar com B_max = 75 min (ex: 140 min -> 2 de 70; 160 min -> 3 de 54).
    """
    # Casos <= 90 min
    assert calculate_cognitive_chunks(45) == (1, 45)
    assert calculate_cognitive_chunks(60) == (1, 60)
    assert calculate_cognitive_chunks(90) == (1, 90)

    # Casos > 90 min (conforme explicitado na especificação)
    # 140 min -> ceil(140/75) = 2 blocos; ceil(140/2) = 70 min
    assert calculate_cognitive_chunks(140) == (2, 70)

    # 160 min -> ceil(160/75) = 3 blocos; ceil(160/3) = 54 min
    assert calculate_cognitive_chunks(160) == (3, 54)

    # 225 min -> ceil(225/75) = 3 blocos; ceil(225/3) = 75 min
    assert calculate_cognitive_chunks(225) == (3, 75)

    # 250 min -> ceil(250/75) = 4 blocos; ceil(250/4) = 63 min
    assert calculate_cognitive_chunks(250) == (4, 63)


def test_busy_intervals_merging_and_window_subtraction():
    """Valida a fusão correta de blocos ocupados sobrepostos/adjacentes e a subtração conjuntista."""
    t_base = datetime(2026, 10, 20, 8, 0, tzinfo=timezone.utc)

    # Intervalos com sobreposição e adjacência
    raw_busy = [
        (t_base + timedelta(minutes=60), t_base + timedelta(minutes=120)),   # 09:00 - 10:00
        (t_base + timedelta(minutes=105), t_base + timedelta(minutes=150)),  # 09:45 - 10:30 (sobrepõe)
        (t_base + timedelta(minutes=150), t_base + timedelta(minutes=180)),  # 10:30 - 11:00 (adjacente)
        (t_base + timedelta(minutes=240), t_base + timedelta(minutes=300)),  # 12:00 - 13:00 (disjunto)
    ]

    merged = merge_busy_intervals(raw_busy)
    # Devem resultar em 2 intervalos consolidados: 09:00 - 11:00 e 12:00 - 13:00
    assert len(merged) == 2
    assert merged[0] == (t_base + timedelta(minutes=60), t_base + timedelta(minutes=180))
    assert merged[1] == (t_base + timedelta(minutes=240), t_base + timedelta(minutes=300))

    # Janela da manhã: 08:30 (t_base + 30m) às 11:30 (t_base + 210m)
    morning_window = (t_base + timedelta(minutes=30), t_base + timedelta(minutes=210))
    free_segments = subtract_busy_from_window(morning_window, merged)

    # Segmentos livres esperados:
    # 1. 08:30 às 09:00 (30 minutos)
    # 2. 11:00 às 11:30 (30 minutos)
    assert len(free_segments) == 2
    assert free_segments[0] == (t_base + timedelta(minutes=30), t_base + timedelta(minutes=60))
    assert free_segments[1] == (t_base + timedelta(minutes=180), t_base + timedelta(minutes=210))


def test_mandatory_10min_transition_buffer():
    """Valida o buffer obrigatório de 10 minutos em cada extremidade do bloco alocado."""
    engine = DeterministicSchedulerEngine(buffer_min=10)

    # Data de teste: segunda-feira às 08:30
    now = datetime(2026, 10, 20, 8, 30, tzinfo=timezone.utc)
    # Prazo: 3 dias depois às 18:00
    due = datetime(2026, 10, 23, 18, 0, tzinfo=timezone.utc)

    # Uma janela de 70 min comporta um bloco de 50 min porque 50 + 20 <= 70
    # Mas uma janela de 65 min NÃO comporta um bloco de 50 min porque 50 + 20 = 70 > 65
    plan = engine.compute_schedule(
        task_id="task-buffer-test",
        est_duration_min=50,  # <= 90 -> 1 bloco de 50 min
        due_datetime=due,
        now=now,
        busy_intervals=[],
    )

    assert len(plan.blocks) == 1
    block = plan.blocks[0]

    # O bloco deve iniciar exatamente 10 min após a abertura da janela permitida (08:30 + 10m = 08:40)
    assert block.start_time == datetime(2026, 10, 20, 8, 40, tzinfo=timezone.utc)
    # O término do bloco é às 08:40 + 50m = 09:30
    assert block.end_time == datetime(2026, 10, 20, 9, 30, tzinfo=timezone.utc)
    assert block.duration_min == 50


def test_antivespera_24h_margin_and_insufficient_window():
    """Valida a restrição antivéspera (24h) e o disparo de InsufficientScheduleWindowError."""
    engine = DeterministicSchedulerEngine()

    now = datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc)

    # Caso 1: due_datetime a menos de 24h de now
    due_short = datetime(2026, 10, 20, 20, 0, tzinfo=timezone.utc)  # apenas 10h à frente
    with pytest.raises(InsufficientScheduleWindowError) as exc_info:
        engine.compute_schedule(
            task_id="task-urgent",
            est_duration_min=60,
            due_datetime=due_short,
            now=now,
        )
    assert "antivéspera" in str(exc_info.value).lower() or "insuficiente" in str(exc_info.value).lower()

    # Caso 2: due_datetime com 48h -> T_alvo_fim é exatamente 24h antes
    due_valid = datetime(2026, 10, 22, 10, 0, tzinfo=timezone.utc)
    plan = engine.compute_schedule(
        task_id="task-valid-window",
        est_duration_min=60,
        due_datetime=due_valid,
        now=now,
    )
    t_alvo_fim = due_valid - timedelta(hours=24)  # 2026-10-21 10:00
    for b in plan.blocks:
        assert b.end_time <= t_alvo_fim


def test_schedule_capacity_exceeded_error_when_saturated():
    """Garante o disparo de ScheduleCapacityExceededError em cenários de agenda 100% saturada."""
    engine = DeterministicSchedulerEngine()

    now = datetime(2026, 10, 20, 8, 30, tzinfo=timezone.utc)
    due = datetime(2026, 10, 21, 18, 0, tzinfo=timezone.utc)
    t_alvo_fim = due - timedelta(hours=24)  # 2026-10-20 18:00

    # Ocupa totalmente o dia 20 das 08:30 às 21:00
    full_day_busy = [
        (datetime(2026, 10, 20, 8, 30, tzinfo=timezone.utc), datetime(2026, 10, 20, 21, 0, tzinfo=timezone.utc))
    ]

    with pytest.raises(ScheduleCapacityExceededError) as exc_info:
        engine.compute_schedule(
            task_id="task-saturated",
            est_duration_min=60,
            due_datetime=due,
            now=now,
            busy_intervals=full_day_busy,
        )
    assert "capacidade de agenda excedida" in str(exc_info.value).lower()


def test_calendar_sync_and_persistence_mocked(memory_db):
    """Valida a persistência em calendar_allocations, status 'SCHEDULED' e chamadas mockadas do Google Calendar."""
    session = memory_db
    now = datetime(2026, 10, 20, 8, 30, tzinfo=timezone.utc)
    due = datetime(2026, 10, 26, 18, 0, tzinfo=timezone.utc)  # 6 dias à frente

    # Popula dados prévios
    course = Course(id="course-so-202", name="Sistemas Operacionais", is_active=True)
    task = Task(
        id="task-threads-101",
        course_id="course-so-202",
        title="Implementação de Semáforos e Mutex",
        due_datetime=due,
        est_duration_min=140,  # 140 min -> 2 blocos de 70 min
        group_required=False,
        status="PENDING",
    )
    material = Material(
        id="drive-mat-pdf-threads",
        course_id="course-so-202",
        title="Guia Prático de POSIX Threads",
        page_count=50,
        lexical_density=1.4,
    )
    session.add_all([course, task, material])
    session.commit()

    # Mocks do Google Calendar
    mock_cal_service = MagicMock()
    mock_freebusy = MagicMock()
    mock_freebusy.query.return_value.execute.return_value = {
        "calendars": {"primary": {"busy": []}}
    }
    mock_cal_service.freebusy.return_value = mock_freebusy

    mock_events = MagicMock()
    mock_events.insert.return_value.execute.side_effect = [
        {"id": "gcal-evt-bloco-1"},
        {"id": "gcal-evt-bloco-2"},
    ]
    mock_cal_service.events.return_value = mock_events

    # Executa a sincronização
    allocations = sync_task_to_google_calendar(
        db_session=session,
        task_id="task-threads-101",
        calendar_service=mock_cal_service,
        now=now,
    )

    # Verificações
    assert len(allocations) == 2
    assert allocations[0].gcal_event_id == "gcal-evt-bloco-1"
    assert allocations[1].gcal_event_id == "gcal-evt-bloco-2"

    # Confirma status da tarefa atualizado no banco
    db_task = session.query(Task).filter(Task.id == "task-threads-101").first()
    assert db_task.status == "SCHEDULED"

    # Confirma chamadas ao events.insert
    assert mock_events.insert.call_count == 2
    first_call_args = mock_events.insert.call_args_list[0][1]["body"]
    assert "[Estudo] Implementação de Semáforos e Mutex (Bloco 1/2)" in first_call_args["summary"]
    assert "Meta: 25 páginas" in first_call_args["description"]
    assert "drive-mat-pdf-threads" in first_call_args["description"]

    # Validação de Idempotência: segunda execução não reinsere eventos nem duplica registros
    repeat_allocations = sync_task_to_google_calendar(
        db_session=session,
        task_id="task-threads-101",
        calendar_service=mock_cal_service,
        now=now,
    )
    assert len(repeat_allocations) == 2
    # call_count continua 2 (não foi chamado novamente)
    assert mock_events.insert.call_count == 2


def test_mixed_naive_and_aware_datetimes_no_typeerror():
    """Regressão QA (Camada C.1): Garante que combinar due_datetime offset-naive (vindo do SQLite)
    com now offset-aware (UTC) e busy_intervals mistos nunca levanta TypeError."""
    engine = DeterministicSchedulerEngine(buffer_min=10)

    # due_datetime sem tzinfo (como retornado por colunas DateTime do SQLite)
    naive_due = datetime(2026, 10, 25, 23, 59, 0)
    # now com tzinfo=timezone.utc
    aware_now = datetime(2026, 10, 20, 8, 0, 0, tzinfo=timezone.utc)
    # intervalos mistos (um naive, um aware)
    mixed_busy = [
        (datetime(2026, 10, 20, 8, 30, 0), datetime(2026, 10, 20, 10, 0, 0)),
        (datetime(2026, 10, 20, 14, 0, 0, tzinfo=timezone.utc), datetime(2026, 10, 20, 15, 0, 0, tzinfo=timezone.utc)),
    ]

    plan = engine.compute_schedule(
        task_id="task-tz-mix",
        est_duration_min=60,
        due_datetime=naive_due,
        now=aware_now,
        busy_intervals=mixed_busy,
    )

    assert len(plan.blocks) == 1
    # Deve alocar na manhã do dia 20 após o ocupado das 08:30-10:00 + 10 min buffer -> 10:10 às 11:10
    assert plan.blocks[0].start_time == datetime(2026, 10, 20, 10, 10, 0, tzinfo=timezone.utc)
    assert plan.blocks[0].end_time == datetime(2026, 10, 20, 11, 10, 0, tzinfo=timezone.utc)

