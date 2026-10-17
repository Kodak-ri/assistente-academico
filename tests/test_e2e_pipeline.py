"""
test_e2e_pipeline.py
Teste de Fumaça Ponta a Ponta (End-to-End Integration Test) — SPEC-001 a SPEC-004.

Valida o fluxo integrado completo:
1. Inicialização do SQLite em memória e subida da aplicação FastAPI com lifespan (APScheduler).
2. Ingestão do Google Classroom (triagem tripartida) -> Leitura de PDF de 30 páginas em memória (PyMuPDF) ->
   Avaliação semântica Gemini -> Cálculo de est_duration_min -> Alocação no Google Calendar com buffers ->
   Persistência em calendar_allocations e disparo de alerta ntfy.
3. Consulta ao painel GET /api/dashboard/daily validando contrato JSON do PWA (blocos, 30 páginas, rotinas).
4. Execução dos callbacks POST /snooze?min=60, POST /tasks/{id}/complete e POST /routines/{id}/complete.
"""

import io
import math
from datetime import datetime, date, timedelta, timezone
from unittest.mock import MagicMock, patch

import httpx
import pymupdf
import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.api.endpoints import get_db
from backend.app.core.database import init_db
from backend.app.main import app, lifespan, run_periodic_scan, run_daily_briefing
from backend.app.models.db_models import (
    Course,
    Material,
    Task,
    CalendarAllocation,
    DomesticRoutine,
)
from backend.app.models.schemas import SemanticTaskEvaluation
from backend.app.services.google_client import GoogleWorkspaceClient
from backend.app.services.notifier import NtfyNotifier


def _create_in_memory_pdf(num_pages: int = 30) -> io.BytesIO:
    """Gera um documento PDF sintético de 30 páginas 100% em memória."""
    with pymupdf.open() as doc:
        for idx in range(num_pages):
            page = doc.new_page()
            page.insert_text(
                (50, 72),
                f"Page {idx + 1} of {num_pages}: Distributed Consensus and Paxos Protocol. "
                f"Group project and seminar presentation required.",
            )
        pdf_bytes = doc.tobytes()
    return io.BytesIO(pdf_bytes)


@pytest.mark.anyio
async def test_full_e2e_pipeline_smoke():
    """Executa o fluxo ponta a ponta completo (SPEC-001 a SPEC-004)."""
    # =========================================================================
    # 1. INICIALIZAÇÃO DO BANCO EM MEMÓRIA E LIFESPAN FASTAPI + APSCHEDULER
    # =========================================================================
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    init_db(target_engine=engine)
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    db_session = TestingSessionLocal()

    # Pré-cadastra uma rotina doméstica para validar registro no APScheduler e no PWA
    routine = DomesticRoutine(
        name="Trocar Roupa de Cama",
        cron_expression="0 20 * * 2,4",
        last_completed=None,
    )
    db_session.add(routine)
    db_session.commit()
    routine_id = routine.id

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db

    # Captura requisições enviadas ao ntfy
    ntfy_requests = []

    def mock_ntfy_handler(request: httpx.Request) -> httpx.Response:
        ntfy_requests.append(request)
        return httpx.Response(200, json={"id": f"ntfy-{len(ntfy_requests)}"})

    ntfy_transport = httpx.MockTransport(mock_ntfy_handler)

    ref_now = datetime(2026, 10, 20, 8, 0, 0, tzinfo=timezone.utc)
    due_dt = datetime(2026, 10, 28, 23, 59, 0, tzinfo=timezone.utc)

    try:
        with patch("backend.app.main.init_db"), \
             patch("backend.app.main.SessionLocal", side_effect=TestingSessionLocal):
            async with lifespan(app):
                # Verifica que o APScheduler subiu com periodic_scan, daily_briefing e a rotina doméstica
                scheduler = getattr(app.state, "scheduler", None)
                assert scheduler is not None
                assert scheduler.running is True
                job_ids = {j.id for j in scheduler.get_jobs()}
                assert {"periodic_scan", "daily_briefing", f"routine_{routine_id}"}.issubset(job_ids)

                # =========================================================================
                # 2. MOCKS DO GOOGLE WORKSPACE (CLASSROOM, DRIVE, CALENDAR) E GEMINI
                # =========================================================================
                mock_classroom = MagicMock()
                mock_drive = MagicMock()
                mock_calendar = MagicMock()

                # Classroom retorna 2 turmas: 1 ativa recente e 1 antiga (> 180 dias)
                mock_classroom.courses().list().execute.return_value = {
                    "courses": [
                        {
                            "id": "course-sd-2026",
                            "name": "Sistemas Distribuídos Avançados",
                            "creationTime": (ref_now - timedelta(days=20)).isoformat(),
                        },
                        {
                            "id": "course-old-2024",
                            "name": "Introdução à Computação 2024",
                            "creationTime": (ref_now - timedelta(days=300)).isoformat(),
                        },
                    ]
                }

                # Anúncios vazios
                mock_classroom.courses().announcements().list().execute.return_value = {
                    "announcements": []
                }

                # CourseWork da turma ativa com anexo PDF de 30 páginas
                mock_classroom.courses().courseWork().list().execute.return_value = {
                    "courseWork": [
                        {
                            "id": "task-paxos-30p",
                            "title": "Artigo Paxos & Seminário em Dupla",
                            "updateTime": (ref_now - timedelta(days=2)).isoformat(),
                            "dueDate": {"year": 2026, "month": 10, "day": 28},
                            "dueTime": {"hours": 23, "minutes": 59},
                            "materials": [
                                {
                                    "driveFile": {
                                        "driveFile": {
                                            "id": "drive-pdf-paxos-30p",
                                            "title": "paxos_made_simple_30p.pdf",
                                            "alternateLink": "https://drive.google.com/file/d/drive-pdf-paxos-30p/view",
                                        }
                                    }
                                }
                            ],
                        }
                    ]
                }

                # Mock do Gemini retornando Structured Output
                mock_gemini = MagicMock()
                expected_eval = SemanticTaskEvaluation(
                    lexical_density=1.2,
                    is_foreign_language=True,
                    group_required=True,
                    production_time_min=20,
                    reasoning_summary="Artigo técnico em inglês (30 págs) exigindo apresentação em dupla.",
                )
                mock_gemini_resp = MagicMock()
                mock_gemini_resp.parsed = expected_eval
                mock_gemini.models.generate_content.return_value = mock_gemini_resp

                # Mock do Google Calendar (Aula ocupando 08:30-10:00 no dia 2026-10-20)
                mock_calendar.freebusy().query().execute.return_value = {
                    "calendars": {
                        "primary": {
                            "busy": [
                                {
                                    "start": "2026-10-20T08:30:00Z",
                                    "end": "2026-10-20T10:00:00Z",
                                }
                            ]
                        }
                    }
                }
                created_gcal_events = []

                def mock_insert_event(calendarId, body):
                    req = MagicMock()
                    idx = len(created_gcal_events) + 1
                    evt_id = f"gcal-e2e-evt-{idx}"
                    created_gcal_events.append((evt_id, body))
                    req.execute.return_value = {"id": evt_id}
                    return req

                mock_calendar.events().insert.side_effect = mock_insert_event

                ws_client = GoogleWorkspaceClient(
                    classroom_service=mock_classroom,
                    drive_service=mock_drive,
                    calendar_service=mock_calendar,
                )
                # Substitui download_drive_pdf_to_memory para retornar nosso PDF de 30 páginas em memória
                ws_client.download_drive_pdf_to_memory = MagicMock(  # type: ignore[method-assign]
                    side_effect=lambda file_id: _create_in_memory_pdf(30)
                )

                async with httpx.AsyncClient(transport=ntfy_transport) as ntfy_http:
                    notifier = NtfyNotifier(
                        server_url="https://ntfy.sh",
                        topic="e2e-academic",
                        public_api_base_url="http://testserver",
                        client=ntfy_http,
                    )

                    # Executa a rotina completa de sincronização e agendamento
                    await run_periodic_scan(
                        notifier=notifier,
                        db_session=db_session,
                        workspace_client=ws_client,
                        gemini_client=mock_gemini,
                        reference_now=ref_now,
                    )

                    # Verifica idempotência: segunda execução não duplica eventos nem tarefas
                    await run_periodic_scan(
                        notifier=notifier,
                        db_session=db_session,
                        workspace_client=ws_client,
                        gemini_client=mock_gemini,
                        reference_now=ref_now,
                    )

                    # Dispara também o briefing diário das 07:30 para validar integração ponta a ponta
                    await run_daily_briefing(
                        notifier=notifier,
                        db_session=db_session,
                        reference_date=date(2026, 10, 20),
                    )

                # Validações da Camada B (Ingestão + Matemática de Esforço)
                # T_total = math.ceil(30 * 3.5 * 1.2 * 1.4) + 20 = math.ceil(176.4) + 20 = 177 + 20 = 197 min
                expected_duration = math.ceil(30 * 3.5 * 1.2 * 1.4) + 20
                assert expected_duration == 197

                db_task = db_session.query(Task).filter(Task.id == "task-paxos-30p").first()
                assert db_task is not None
                assert db_task.est_duration_min == 197
                assert db_task.group_required is True
                assert db_task.status == "SCHEDULED"

                db_mat = db_session.query(Material).filter(Material.id == "drive-pdf-paxos-30p").first()
                assert db_mat is not None
                assert db_mat.page_count == 30
                assert db_mat.lexical_density == 1.2

                # Validações da Camada C (Fracionamento Cognitivo e Buffers no Google Calendar)
                # 197 > 90 -> k = ceil(197 / 75) = 3 blocos de ceil(197 / 3) = 66 min
                allocs = (
                    db_session.query(CalendarAllocation)
                    .filter(CalendarAllocation.task_id == "task-paxos-30p")
                    .order_by(CalendarAllocation.start_time.asc())
                    .all()
                )
                assert len(allocs) == 3
                assert len(created_gcal_events) == 3

                # No dia 2026-10-20, 08:30-10:00 está ocupado; com buffer de 10 min, o Bloco 1 começa às 10:10
                first_start = allocs[0].start_time
                if first_start.tzinfo is None:
                    first_start = first_start.replace(tzinfo=timezone.utc)
                assert first_start == datetime(2026, 10, 20, 10, 10, 0, tzinfo=timezone.utc)
                first_end = allocs[0].end_time
                if first_end.tzinfo is None:
                    first_end = first_end.replace(tzinfo=timezone.utc)
                assert first_end == datetime(2026, 10, 20, 11, 16, 0, tzinfo=timezone.utc)

                # Nenhum bloco termina após due_datetime - 24h
                antivespera_limit = due_dt - timedelta(hours=24)
                for a in allocs:
                    a_end = a.end_time.replace(tzinfo=timezone.utc) if a.end_time.tzinfo is None else a.end_time
                    assert a_end <= antivespera_limit

                # Valida que o alerta urgente de grupo e o briefing diário foram enviados ao ntfy
                assert len(ntfy_requests) == 2
                assert ntfy_requests[0].headers["Priority"] == "5"
                assert "Trabalho em Grupo" in ntfy_requests[0].headers["Title"]
                assert "Despacho Diário" in ntfy_requests[1].headers["Title"]

                # =========================================================================
                # 3. VALIDAÇÃO DO PAINEL DIÁRIO (GET /api/dashboard/daily) PARA O PWA
                # =========================================================================
                asgi_transport = ASGITransport(app=app)
                async with AsyncClient(transport=asgi_transport, base_url="http://testserver") as client:
                    dash_resp = await client.get("/api/dashboard/daily?date=2026-10-20")
                    assert dash_resp.status_code == 200
                    dash = dash_resp.json()

                    assert dash["date"] == "2026-10-20"
                    assert len(dash["classes_and_allocations"]) == 1
                    block_item = dash["classes_and_allocations"][0]
                    assert block_item["task_id"] == "task-paxos-30p"
                    assert block_item["course_name"] == "Sistemas Distribuídos Avançados"
                    assert "10:10:00" in block_item["start_time"]
                    assert block_item["status"] == "SCHEDULED"

                    assert len(dash["reading_queue"]) == 1
                    read_item = dash["reading_queue"][0]
                    assert read_item["task_id"] == "task-paxos-30p"
                    assert read_item["page_count"] == 30
                    assert read_item["est_duration_min"] == 197
                    assert read_item["group_required"] is True
                    assert "drive-pdf-paxos-30p" in read_item["drive_url"]

                    assert len(dash["domestic_routines"]) == 1
                    rout_item = dash["domestic_routines"][0]
                    assert rout_item["id"] == routine_id
                    assert rout_item["name"] == "Trocar Roupa de Cama"
                    assert rout_item["is_completed_today"] is False

                    # =====================================================================
                    # 4. ACIONAMENTO DOS CALLBACKS DE MUTAÇÃO (SNOOZE & COMPLETE)
                    # =====================================================================
                    # 4.1 POST /api/tasks/{id}/snooze?min=60
                    with patch("backend.app.api.endpoints.datetime") as mock_dt:
                        mock_dt.now.return_value = datetime(2026, 10, 20, 10, 0, 0, tzinfo=timezone.utc)
                        mock_dt.combine = datetime.combine
                        mock_dt.fromisoformat = datetime.fromisoformat
                        snooze_resp = await client.post("/api/tasks/task-paxos-30p/snooze?min=60")

                    assert snooze_resp.status_code == 200
                    snooze_data = snooze_resp.json()
                    assert snooze_data["status"] == "success"
                    assert snooze_data["new_allocations_count"] == 3
                    # Com snooze de 60 min a partir das 10:00 (new_now = 11:00), o primeiro bloco
                    # vai para a janela da tarde (14:10)
                    assert "14:10:00" in snooze_data["allocations"][0]["start_time"]

                    # 4.2 POST /api/tasks/{id}/complete
                    comp_task_resp = await client.post("/api/tasks/task-paxos-30p/complete")
                    assert comp_task_resp.status_code == 200
                    assert comp_task_resp.json()["task_status"] == "COMPLETED"

                    db_session.expire_all()
                    updated_task = db_session.query(Task).filter(Task.id == "task-paxos-30p").first()
                    assert updated_task is not None
                    assert updated_task.status == "COMPLETED"

                    # 4.3 POST /api/routines/{id}/complete
                    comp_rout_resp = await client.post(f"/api/routines/{routine_id}/complete")
                    assert comp_rout_resp.status_code == 200
                    assert comp_rout_resp.json()["routine_id"] == routine_id
                    assert comp_rout_resp.json()["last_completed"] is not None

                    db_session.expire_all()
                    updated_routine = (
                        db_session.query(DomesticRoutine)
                        .filter(DomesticRoutine.id == routine_id)
                        .first()
                    )
                    assert updated_routine is not None
                    assert updated_routine.last_completed is not None

                    # 4.4 Verifica reflexo imediato no GET /api/dashboard/daily após conclusões
                    today_tz_str = datetime.now(timezone.utc).date().isoformat()
                    dash_after_resp = await client.get(f"/api/dashboard/daily?date={today_tz_str}")
                    assert dash_after_resp.status_code == 200
                    dash_after = dash_after_resp.json()
                    # A tarefa concluída não deve mais constar na reading_queue pendente
                    assert len(dash_after["reading_queue"]) == 0
    finally:
        app.dependency_overrides.clear()
        db_session.close()
