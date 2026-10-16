import asyncio
from datetime import datetime, date, time, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import sessionmaker

from backend.app.api.endpoints import get_db
from backend.app.core.database import Base, init_db
from backend.app.main import app, lifespan
from backend.app.models.db_models import Course, Material, Task, CalendarAllocation, DomesticRoutine
from backend.app.services.notifier import NtfyNotifier


@pytest.fixture
def memory_db():
    """Cria um banco SQLite em memória isolado para os testes da API e notifier."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    init_db(target_engine=engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    session = TestingSession()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
async def async_client(memory_db):
    """Cliente HTTP assíncrono conectado à app FastAPI com injeção do banco em memória."""
    def override_get_db():
        try:
            yield memory_db
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client
    app.dependency_overrides.clear()


# =============================================================================
# 1. TESTES DOS ENDPOINTS REST (TASKS, SNOOZE, ROUTINES, DASHBOARD)
# =============================================================================

@pytest.mark.anyio
async def test_complete_task_endpoint(async_client: AsyncClient, memory_db):
    """Valida transição de estado transacional para COMPLETED via POST /api/tasks/{task_id}/complete."""
    course = Course(id="c1", name="Sistemas Distribuídos", is_active=True)
    task = Task(
        id="task-100",
        course_id="c1",
        title="Laboratório 1",
        due_datetime=datetime.now(timezone.utc) + timedelta(days=5),
        est_duration_min=60,
        status="PENDING",
    )
    memory_db.add_all([course, task])
    memory_db.commit()

    # 1. Sucesso
    resp = await async_client.post("/api/tasks/task-100/complete")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"
    assert data["task_status"] == "COMPLETED"
    assert data["task_id"] == "task-100"

    # Confirmação no banco
    memory_db.expire_all()
    updated_task = memory_db.query(Task).filter(Task.id == "task-100").first()
    assert updated_task is not None
    assert updated_task.status == "COMPLETED"

    # 2. Tarefa Inexistente (404)
    resp_404 = await async_client.post("/api/tasks/nao-existe/complete")
    assert resp_404.status_code == 404
    assert "não encontrada" in resp_404.json()["detail"]


@pytest.mark.anyio
async def test_snooze_task_endpoint_success(async_client: AsyncClient, memory_db):
    """Valida recálculo do agendamento linear com snooze via POST /api/tasks/{task_id}/snooze?min=60."""
    now_utc = datetime(2026, 10, 20, 8, 0, tzinfo=timezone.utc)
    course = Course(id="c2", name="Estruturas de Dados", is_active=True)
    task = Task(
        id="task-200",
        course_id="c2",
        title="Trabalho Prático Árvores",
        # Prazo com folga generosa (> 5 dias) para respeitar margem antivéspera
        due_datetime=now_utc + timedelta(days=6),
        est_duration_min=60,
        status="PENDING",
    )
    memory_db.add_all([course, task])
    memory_db.commit()

    # Dispara snooze de 60 minutos
    with patch("backend.app.api.endpoints.datetime") as mock_dt:
        mock_dt.now.return_value = now_utc
        mock_dt.combine = datetime.combine
        mock_dt.fromisoformat = datetime.fromisoformat
        resp = await async_client.post("/api/tasks/task-200/snooze?min=60")

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"
    assert data["task_id"] == "task-200"
    assert data["new_allocations_count"] >= 1
    assert len(data["allocations"]) >= 1

    # Confirmação de persistência no banco
    memory_db.expire_all()
    allocations = memory_db.query(CalendarAllocation).filter(CalendarAllocation.task_id == "task-200").all()
    start_dt = allocations[0].start_time
    if start_dt.tzinfo is None:
        start_dt = start_dt.replace(tzinfo=timezone.utc)
    assert start_dt >= (now_utc + timedelta(minutes=60))


@pytest.mark.anyio
async def test_snooze_task_conflict_insufficient_window(async_client: AsyncClient, memory_db):
    """Valida resposta 409 Conflict quando o adiamento viola a margem de 24h antivéspera."""
    now_utc = datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc)
    course = Course(id="c3", name="Compiladores", is_active=True)
    # Prazo para daqui a 24 horas: com snooze de 60 min, now + 60 min viola a margem antivéspera
    task = Task(
        id="task-300",
        course_id="c3",
        title="Entrega Imediata",
        due_datetime=now_utc + timedelta(hours=24, minutes=30),
        est_duration_min=60,
        status="PENDING",
    )
    memory_db.add_all([course, task])
    memory_db.commit()

    with patch("backend.app.api.endpoints.datetime") as mock_dt:
        mock_dt.now.return_value = now_utc
        mock_dt.combine = datetime.combine
        mock_dt.fromisoformat = datetime.fromisoformat
        resp = await async_client.post("/api/tasks/task-300/snooze?min=60")

    assert resp.status_code == 409
    assert "margem antivéspera" in resp.json()["detail"]


@pytest.mark.anyio
async def test_complete_routine_endpoint(async_client: AsyncClient, memory_db):
    """Valida atualização de last_completed via POST /api/routines/{routine_id}/complete."""
    routine = DomesticRoutine(
        name="Limpeza da Cozinha",
        cron_expression="0 20 * * 2,4",
        last_completed=None,
    )
    memory_db.add(routine)
    memory_db.commit()
    r_id = routine.id

    # 1. Sucesso
    resp = await async_client.post(f"/api/routines/{r_id}/complete")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"
    assert data["routine_id"] == r_id
    assert data["last_completed"] is not None

    # Confirmação no banco
    memory_db.expire_all()
    updated_routine = memory_db.query(DomesticRoutine).filter(DomesticRoutine.id == r_id).first()
    assert updated_routine is not None
    assert updated_routine.last_completed is not None

    # 2. Rotina inexistente (404)
    resp_404 = await async_client.post("/api/routines/9999/complete")
    assert resp_404.status_code == 404


@pytest.mark.anyio
async def test_daily_dashboard_endpoint(async_client: AsyncClient, memory_db):
    """Valida o payload estruturado completo retornado por GET /api/dashboard/daily."""
    ref_date = date(2026, 10, 21)
    c = Course(id="c10", name="Engenharia de Software")
    m = Material(id="m10", course_id="c10", title="Artigo Microservices", page_count=35)
    t = Task(
        id="t10",
        course_id="c10",
        title="Fichamento Arquitetura",
        due_datetime=datetime(2026, 10, 25, 23, 59, tzinfo=timezone.utc),
        est_duration_min=90,
        status="SCHEDULED",
    )
    alloc = CalendarAllocation(
        task_id="t10",
        gcal_event_id="gcal-event-10",
        start_time=datetime(2026, 10, 21, 9, 0, tzinfo=timezone.utc),
        end_time=datetime(2026, 10, 21, 10, 30, tzinfo=timezone.utc),
    )
    routine = DomesticRoutine(
        name="Regar Plantas",
        cron_expression="0 8 * * *",
        last_completed=datetime(2026, 10, 21, 8, 15, tzinfo=timezone.utc),
    )
    memory_db.add_all([c, m, t, alloc, routine])
    memory_db.commit()

    resp = await async_client.get(f"/api/dashboard/daily?date={ref_date.isoformat()}")
    assert resp.status_code == 200
    data = resp.json()

    assert data["date"] == "2026-10-21"

    # 1. Alocações
    assert len(data["classes_and_allocations"]) == 1
    alloc_item = data["classes_and_allocations"][0]
    assert alloc_item["task_id"] == "t10"
    assert alloc_item["course_name"] == "Engenharia de Software"
    assert "09:00:00" in alloc_item["start_time"]

    # 2. Fila de Leitura
    assert len(data["reading_queue"]) == 1
    queue_item = data["reading_queue"][0]
    assert queue_item["task_id"] == "t10"
    assert queue_item["page_count"] == 35

    # 3. Rotinas Domésticas
    assert len(data["domestic_routines"]) == 1
    r_item = data["domestic_routines"][0]
    assert r_item["name"] == "Regar Plantas"
    assert r_item["is_completed_today"] is True


@pytest.mark.anyio
async def test_daily_dashboard_invalid_date(async_client: AsyncClient):
    """Valida erro 400 Bad Request ao fornecer data fora do padrão ISO."""
    resp = await async_client.get("/api/dashboard/daily?date=invalid-date")
    assert resp.status_code == 400
    assert "Formato de data inválido" in resp.json()["detail"]


# =============================================================================
# 2. TESTES DO NOTIFIER NTFY (HEADERS, ACTIONS, RETRY COM BACKOFF)
# =============================================================================

@pytest.mark.anyio
async def test_notifier_study_block_alert_headers_and_actions():
    """Valida os cabeçalhos HTTP padronizados e as ações ntfy para alerta de estudo."""
    captured_requests = []

    def mock_transport_handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(200, json={"id": "msg-123"})

    transport = httpx.MockTransport(mock_transport_handler)
    async with httpx.AsyncClient(transport=transport) as test_http_client:
        notifier = NtfyNotifier(
            server_url="https://ntfy.sh",
            topic="test-study",
            public_api_base_url="https://api.meudominio.com",
            client=test_http_client,
        )

        ok = await notifier.send_study_block_alert(
            task_id="task-42",
            course_name="Redes II",
            task_title="Simulação BGP",
            block_index=1,
            total_blocks=2,
            duration_min=50,
            material_url="https://drive.google.com/file/d/xyz/view",
        )
        assert ok is True

    assert len(captured_requests) == 1
    req = captured_requests[0]
    assert req.url == "https://ntfy.sh/test-study"
    assert req.headers["Title"] == "Bloco de Estudo: Redes II"
    assert req.headers["Priority"] == "5"
    assert req.headers["Tags"] == "books,hourglass_flowing_sand"

    actions_header = req.headers["Actions"]
    assert "http, Concluir, https://api.meudominio.com/api/tasks/task-42/complete, method=POST, clear=true" in actions_header
    assert "http, Adiar 1h, https://api.meudominio.com/api/tasks/task-42/snooze?min=60, method=POST, clear=true" in actions_header
    assert "view, Abrir PDF, https://drive.google.com/file/d/xyz/view" in actions_header


@pytest.mark.anyio
async def test_notifier_domestic_routine_alert_contract():
    """Valida contrato ntfy para disparo de rotina doméstica."""
    captured_requests = []

    def mock_transport_handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(200, json={"id": "routine-msg"})

    transport = httpx.MockTransport(mock_transport_handler)
    async with httpx.AsyncClient(transport=transport) as test_http_client:
        notifier = NtfyNotifier(
            server_url="https://ntfy.sh",
            topic="test-routine",
            public_api_base_url="http://localhost:8000",
            client=test_http_client,
        )

        ok = await notifier.send_domestic_routine_alert(routine_id=7, routine_name="Levar Lixo")
        assert ok is True

    assert len(captured_requests) == 1
    req = captured_requests[0]
    assert req.headers["Title"] == "Rotina Doméstica: Levar Lixo"
    assert req.headers["Priority"] == "5"
    assert req.headers["Tags"] == "house,sparkles"
    assert "http, Concluir, http://localhost:8000/api/routines/7/complete, method=POST, clear=true" in req.headers["Actions"]


@pytest.mark.anyio
async def test_notifier_group_urgent_alert():
    """Valida cabeçalhos e tags de prioridade máxima para trabalho em grupo."""
    captured_requests = []

    def mock_transport_handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(200, json={"id": "group-msg"})

    transport = httpx.MockTransport(mock_transport_handler)
    async with httpx.AsyncClient(transport=transport) as test_http_client:
        notifier = NtfyNotifier(
            server_url="https://ntfy.sh",
            topic="test-group",
            public_api_base_url="http://localhost:8000",
            client=test_http_client,
        )

        ok = await notifier.send_group_urgent_alert(
            task_id="t-grp",
            task_title="Seminário de Segurança",
            course_name="Criptografia",
            due_datetime=datetime(2026, 11, 1, 18, 0),
            reasoning="Enunciado exige formação de duplas ou trios.",
        )
        assert ok is True

    assert len(captured_requests) == 1
    req = captured_requests[0]
    assert req.headers["Title"] == "Alerta Urgente: Trabalho em Grupo Detectado"
    assert req.headers["Priority"] == "5"
    assert "warning" in req.headers["Tags"]
    assert "people_hugging" in req.headers["Tags"]
    assert "http, Concluir, http://localhost:8000/api/tasks/t-grp/complete" in req.headers["Actions"]


@pytest.mark.anyio
async def test_notifier_oauth_emergency_alert():
    """Valida envio de alerta crítico de OAuth2."""
    captured_requests = []

    def mock_transport_handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(200, json={"id": "oauth-msg"})

    transport = httpx.MockTransport(mock_transport_handler)
    async with httpx.AsyncClient(transport=transport) as test_http_client:
        notifier = NtfyNotifier(
            server_url="https://ntfy.sh",
            topic="test-oauth",
            client=test_http_client,
        )

        ok = await notifier.send_oauth_emergency_alert("invalid_grant: Token has been expired or revoked.")
        assert ok is True

    assert len(captured_requests) == 1
    req = captured_requests[0]
    assert req.headers["Title"] == "Emergência OAuth2: Falha de Renovação de Token"
    assert req.headers["Priority"] == "5"
    assert "key,skull,warning" in req.headers["Tags"]


@pytest.mark.anyio
async def test_notifier_retry_and_backoff_transient_failure():
    """Valida tolerância a falhas: 2 erros temporários seguidos de sucesso na 3ª tentativa."""
    attempts = 0

    def mock_transport_handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise httpx.ConnectError("Falha transitória de conexão DNS/TCP", request=request)
        return httpx.Response(200, json={"id": "success-attempt-3"})

    transport = httpx.MockTransport(mock_transport_handler)
    async with httpx.AsyncClient(transport=transport) as test_http_client:
        notifier = NtfyNotifier(
            server_url="https://ntfy.sh",
            topic="test-retry",
            client=test_http_client,
            max_retries=3,
            backoff_base=0.01,  # Acelera backoff durante o teste
        )

        ok = await notifier.send_notification("Mensagem com retry")
        assert ok is True
        assert attempts == 3


@pytest.mark.anyio
async def test_notifier_exhausted_retries_resilience():
    """Valida resiliência local: falhas contínuas não quebram a aplicação e retornam False."""
    attempts = 0

    def mock_transport_handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.TimeoutException("Timeout esgotado de 10s", request=request)

    transport = httpx.MockTransport(mock_transport_handler)
    async with httpx.AsyncClient(transport=transport) as test_http_client:
        notifier = NtfyNotifier(
            server_url="https://ntfy.sh",
            topic="test-exhausted",
            client=test_http_client,
            max_retries=3,
            backoff_base=0.01,
        )

        ok = await notifier.send_notification("Mensagem com falha permanente")
        assert ok is False
        assert attempts == 3


# =============================================================================
# 3. TESTES DO LIFESPAN FASTAPI, APSCHEDULER E HEALTHCHECK
# =============================================================================

@pytest.mark.anyio
async def test_healthcheck_endpoint(async_client: AsyncClient):
    """Valida integridade do endpoint GET /health."""
    resp = await async_client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "healthy"
    assert "scheduler_running" in data
    assert "jobs_count" in data


@pytest.mark.anyio
async def test_lifespan_initialization_and_shutdown():
    """Valida que o lifespan inicializa o SQLite e registra os jobs no AsyncIOScheduler."""
    async with lifespan(app):
        scheduler = getattr(app.state, "scheduler", None)
        assert scheduler is not None
        assert scheduler.running is True

        job_ids = [j.id for j in scheduler.get_jobs()]
        # Confirma registro dos jobs da SPEC-003
        assert "periodic_scan" in job_ids
        assert "daily_briefing" in job_ids

    # Confirma shutdown gracioso do scheduler após saída do contexto
    assert scheduler.running is False
