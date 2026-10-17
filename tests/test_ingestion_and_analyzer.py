import io
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock

import pytest
import pymupdf
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

from backend.app.core.database import Base, init_db
from backend.app.models.db_models import Course, Material, Task, CalendarAllocation, DomesticRoutine
from backend.app.models.schemas import SemanticTaskEvaluation
from backend.app.services.google_client import GoogleWorkspaceClient
from backend.app.services.document_analyzer import (
    extract_pdf_info_in_memory,
    calculate_task_effort,
    process_document_and_persist,
)


@pytest.fixture
def memory_db():
    """Cria um banco SQLite em memória isolado para os testes."""
    engine = create_engine("sqlite:///:memory:", future=True)
    init_db(target_engine=engine)
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    session = TestingSessionLocal()
    try:
        yield session, engine
    finally:
        session.close()


def test_schema_table_creation(memory_db):
    """Critério 1: Valida a criação correta das 5 tabelas do esquema SQLAlchemy 2.0."""
    _, engine = memory_db
    inspector = inspect(engine)
    tables = inspector.get_table_names()

    expected_tables = {
        "courses",
        "materials",
        "tasks",
        "calendar_allocations",
        "domestic_routines",
    }
    assert expected_tables.issubset(set(tables)), f"Tabelas criadas: {tables}"

    # Validação detalhada das colunas da tabela courses
    course_cols = {col["name"]: col for col in inspector.get_columns("courses")}
    assert "id" in course_cols
    assert "name" in course_cols
    assert "is_active" in course_cols
    assert "last_synced_at" in course_cols

    # Validação detalhada das colunas da tabela tasks
    task_cols = {col["name"]: col for col in inspector.get_columns("tasks")}
    assert "id" in task_cols
    assert "course_id" in task_cols
    assert "title" in task_cols
    assert "due_datetime" in task_cols
    assert "est_duration_min" in task_cols
    assert "group_required" in task_cols
    assert "status" in task_cols


def test_filter_active_courses_discard_logic(memory_db):
    """Critério 2: Valida o descarte de turmas com mais de 180 dias ou sem atividade nos últimos 45 dias."""
    session, _ = memory_db
    ref_date = datetime(2026, 10, 16, 12, 0, 0, tzinfo=timezone.utc)

    # Mock do serviço Classroom
    mock_classroom = MagicMock()
    client = GoogleWorkspaceClient(classroom_service=mock_classroom)

    # 4 Cenários de teste:
    # 1. Turma Antiga (> 180 dias): criação há 200 dias, sem registro prévio ativo -> deve ser descartada (Filtro 1).
    # 2. Turma Local Inativa: registrada no banco com is_active=False -> deve ser ignorada imediatamente (Filtro 2).
    # 3. Turma Dormente (< 180 dias, sem postagens nos últimos 45 dias) -> classificada como is_active=False e descartada (Filtro 3).
    # 4. Turma Ativa Válida: criação há 60 dias e tarefa recente publicada há 10 dias -> deve ser mantida com is_active=True.

    mock_classroom.courses().list().execute.return_value = {
        "courses": [
            {
                "id": "course-old-200d",
                "name": "Turma Antiga 2025",
                "creationTime": (ref_date - timedelta(days=200)).isoformat(),
            },
            {
                "id": "course-dormant-local",
                "name": "Turma Desativada Manualmente",
                "creationTime": (ref_date - timedelta(days=50)).isoformat(),
            },
            {
                "id": "course-dormant-no-activity",
                "name": "Turma Sem Postagens Recentes",
                "creationTime": (ref_date - timedelta(days=60)).isoformat(),
            },
            {
                "id": "course-active-valid",
                "name": "Sistemas Distribuídos 2026",
                "creationTime": (ref_date - timedelta(days=30)).isoformat(),
            },
        ]
    }

    # Pré-popula Turma 2 com is_active=False no banco local
    session.add(Course(id="course-dormant-local", name="Turma Desativada", is_active=False))
    session.commit()

    # Configuração de anúncios e tarefas por turma
    def mock_announcements_list(courseId):
        mock_req = MagicMock()
        if courseId == "course-active-valid":
            mock_req.execute.return_value = {
                "announcements": [
                    {"id": "ann-1", "updateTime": (ref_date - timedelta(days=5)).isoformat()}
                ]
            }
        elif courseId == "course-dormant-no-activity":
            mock_req.execute.return_value = {
                "announcements": [
                    {"id": "ann-old", "updateTime": (ref_date - timedelta(days=60)).isoformat()}
                ]
            }
        else:
            mock_req.execute.return_value = {"announcements": []}
        return mock_req

    def mock_coursework_list(courseId):
        mock_req = MagicMock()
        if courseId == "course-dormant-no-activity":
            mock_req.execute.return_value = {
                "courseWork": [
                    {"id": "cw-old", "updateTime": (ref_date - timedelta(days=55)).isoformat()}
                ]
            }
        else:
            mock_req.execute.return_value = {"courseWork": []}
        return mock_req

    mock_classroom.courses().announcements().list.side_effect = mock_announcements_list
    mock_classroom.courses().courseWork().list.side_effect = mock_coursework_list

    # Executa a filtragem
    filtered = client.filter_active_courses(db_session=session, reference_date=ref_date)

    # Apenas course-active-valid deve ser aprovada
    assert len(filtered) == 1
    assert filtered[0]["id"] == "course-active-valid"

    # Verifica persistência no banco
    db_active = session.query(Course).filter(Course.id == "course-active-valid").first()
    assert db_active is not None
    assert db_active.is_active is True

    # Verifica que course-dormant-no-activity foi marcada como is_active=False
    db_dormant = session.query(Course).filter(Course.id == "course-dormant-no-activity").first()
    assert db_dormant is not None
    assert db_dormant.is_active is False


def test_in_memory_pdf_reading_no_disk():
    """Critério 3: Valida a leitura de um PDF gerado em memória via fitz sem escrita em disco."""
    # Gera documento PDF em memória com 3 páginas
    doc = pymupdf.open()
    page1 = doc.new_page()
    page1.insert_text((50, 72), "Página 1: Introdução à Arquitetura de Software")
    page2 = doc.new_page()
    page2.insert_text((50, 72), "Página 2: Requisitos e Especificações Técnicas")
    page3 = doc.new_page()
    page3.insert_text((50, 72), "Página 3: Conteúdo excedente não lido no extrato inicial")

    pdf_bytes = doc.tobytes()
    doc.close()

    stream = io.BytesIO(pdf_bytes)

    page_count, extracted_text = extract_pdf_info_in_memory(stream)

    assert page_count == 3
    assert "Página 1: Introdução à Arquitetura de Software" in extracted_text
    assert "Página 2: Requisitos e Especificações Técnicas" in extracted_text
    # As páginas seguintes não devem constar no extrato para o Gemini
    assert "Página 3: Conteúdo excedente" not in extracted_text


def test_effort_calculation_mathematics_and_clamp():
    """Critério 4: Valida o cálculo matemático exato de T_leitura e T_total,
    incluindo o clamp de kappa_densidade em [1.0, 2.2] e a penalização de 1.4 para língua estrangeira:
    T_total = math.ceil(P_total * 3.5 * lexical_density * gamma_lang) + T_prod
    """
    # Caso 1: Leitura em português, densidade padrão (1.0), sem produção
    # T_leitura = math.ceil(10 * 3.5 * 1.0 * 1.0) = 35
    # T_total = 35 + 0 = 35
    eval_base = SemanticTaskEvaluation(
        lexical_density=1.0,
        is_foreign_language=False,
        group_required=False,
        production_time_min=0,
        reasoning_summary="Artigo padrão em português",
    )
    res_base = calculate_task_effort(page_count=10, evaluation=eval_base)
    assert res_base.reading_time_min == 35
    assert res_base.total_duration_min == 35

    # Caso 2: Clamp inferior (kappa < 1.0 deve virar 1.0)
    eval_clamp_low = SemanticTaskEvaluation(
        lexical_density=0.4,
        is_foreign_language=False,
        group_required=False,
        production_time_min=10,
    )
    res_clamp_low = calculate_task_effort(page_count=10, evaluation=eval_clamp_low)
    assert res_clamp_low.lexical_density == 1.0
    assert res_clamp_low.reading_time_min == 35
    assert res_clamp_low.total_duration_min == 45

    # Caso 3: Clamp superior (kappa > 2.2 deve virar 2.2)
    # T_leitura = math.ceil(10 * 3.5 * 2.2 * 1.0) = math.ceil(77.0) = 77
    # T_total = 77 + 30 = 107
    eval_clamp_high = SemanticTaskEvaluation(
        lexical_density=3.5,
        is_foreign_language=False,
        group_required=False,
        production_time_min=30,
    )
    res_clamp_high = calculate_task_effort(page_count=10, evaluation=eval_clamp_high)
    assert res_clamp_high.lexical_density == 2.2
    assert res_clamp_high.reading_time_min == 77
    assert res_clamp_high.total_duration_min == 107

    # Caso 4: Língua estrangeira (gamma_lang = 1.4), kappa = 1.5, N = 8 páginas, T_producao = 20
    # N * 3.5 * kappa * gamma_lang = 8 * 3.5 * 1.5 * 1.4 = 58.8 -> math.ceil = 59
    # T_total = 59 + 20 = 79
    eval_foreign = SemanticTaskEvaluation(
        lexical_density=1.5,
        is_foreign_language=True,
        group_required=False,
        production_time_min=20,
    )
    res_foreign = calculate_task_effort(page_count=8, evaluation=eval_foreign)
    assert res_foreign.reading_time_min == 59
    assert res_foreign.total_duration_min == 79


def test_priority_alert_and_persistence(memory_db):
    """Critério 5: Valida o disparo de alerta quando group_required == True e a persistência relacional."""
    session, _ = memory_db
    mock_alert_hook = MagicMock()

    # Cria documento sintético em memória
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((50, 72), "Enunciado do trabalho em grupo para seminário temático.")
    pdf_bytes = doc.tobytes()
    doc.close()

    eval_group = SemanticTaskEvaluation(
        lexical_density=1.2,
        is_foreign_language=False,
        group_required=True,
        production_time_min=45,
        reasoning_summary="Requer divisão de temas e elaboração de slides em dupla.",
    )

    due_date = datetime(2026, 11, 1, 23, 59, 0, tzinfo=timezone.utc)

    effort = process_document_and_persist(
        pdf_source=pdf_bytes,
        course_id="course-sd-101",
        course_name="Sistemas Distribuídos",
        task_id="task-sem-01",
        task_title="Seminário em Grupo: Algoritmos de Consenso",
        due_datetime=due_date,
        file_id="drive-doc-consensus-pdf",
        db_session=session,
        alert_hook=mock_alert_hook,
        custom_evaluation=eval_group,
    )

    # 1. Verifica disparo do alerta prioritário
    mock_alert_hook.assert_called_once_with(
        "Seminário em Grupo: Algoritmos de Consenso",
        "Requer divisão de temas e elaboração de slides em dupla.",
    )

    # 2. Verifica persistência na tabela courses
    saved_course = session.query(Course).filter(Course.id == "course-sd-101").first()
    assert saved_course is not None
    assert saved_course.name == "Sistemas Distribuídos"
    assert saved_course.is_active is True

    # 3. Verifica persistência na tabela materials
    saved_material = session.query(Material).filter(Material.id == "drive-doc-consensus-pdf").first()
    assert saved_material is not None
    assert saved_material.page_count == 1
    assert saved_material.lexical_density == 1.2

    # 4. Verifica persistência na tabela tasks
    saved_task = session.query(Task).filter(Task.id == "task-sem-01").first()
    assert saved_task is not None
    assert saved_task.group_required is True
    assert saved_task.status == "PENDING"
    assert saved_task.est_duration_min == effort.total_duration_min


def test_no_alert_when_group_not_required(memory_db):
    """Garante que nenhum alerta é disparado quando group_required é False."""
    session, _ = memory_db
    mock_alert_hook = MagicMock()

    doc = pymupdf.open()
    doc.new_page().insert_text((50, 72), "Leitura individual.")
    pdf_bytes = doc.tobytes()
    doc.close()

    eval_solo = SemanticTaskEvaluation(
        lexical_density=1.0,
        is_foreign_language=False,
        group_required=False,
        production_time_min=0,
        reasoning_summary="Leitura individual simples.",
    )

    process_document_and_persist(
        pdf_source=pdf_bytes,
        course_id="course-solo-101",
        course_name="Filosofia da Ciência",
        task_id="task-read-01",
        task_title="Leitura Individual de Artigo",
        due_datetime=datetime.now(timezone.utc),
        file_id="drive-solo-pdf",
        db_session=session,
        alert_hook=mock_alert_hook,
        custom_evaluation=eval_solo,
    )

    mock_alert_hook.assert_not_called()


def test_oauth_refresh_failure_triggers_emergency_alert():
    """Regressão QA (Camada D.3): Garante que falha na renovação do refresh_token OAuth2
    aciona imediatamente o alerta de emergência via ntfy."""
    from unittest.mock import AsyncMock

    mock_creds = MagicMock()
    mock_creds.valid = False
    mock_creds.expired = True
    mock_creds.refresh_token = "mock-refresh-token"
    mock_creds.refresh.side_effect = RuntimeError("invalid_grant: Token has been expired or revoked")

    mock_notifier = MagicMock()
    mock_notifier.send_oauth_emergency_alert = AsyncMock(return_value=True)

    client = GoogleWorkspaceClient(
        token_path="non_existent_token.json",
        credentials_path="non_existent_creds.json",
        classroom_service=MagicMock(),
        drive_service=MagicMock(),
        calendar_service=MagicMock(),
        notifier=mock_notifier,
    )
    client.credentials = mock_creds

    with pytest.raises(RuntimeError, match="invalid_grant"):
        client.authenticate()

    mock_notifier.send_oauth_emergency_alert.assert_called_once()
    call_arg = mock_notifier.send_oauth_emergency_alert.call_args[0][0]
    assert "invalid_grant" in call_arg

