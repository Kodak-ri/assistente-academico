import json
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch
import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

from backend.app.core.database import init_db
from backend.app.models.db_models import Task, Course, Material, DomesticRoutine
from backend.app.models.schemas import SemanticTaskEvaluation
from backend.app.services.llm_router import (
    evaluate_task_with_local_llm,
    evaluate_task_dual_router,
)
from backend.app.services.mcp_server import (
    listar_tarefas_pendentes_unesp,
    consultar_jornada_diaria,
    concluir_tarefa_ou_rotina,
    executar_matriz_ach_ri,
)
from backend.app.main import app


@pytest.fixture
def mock_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    init_db(target_engine=engine)
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    db = TestingSessionLocal()
    with patch("backend.app.services.mcp_server.SessionLocal", side_effect=TestingSessionLocal):
        yield db
    db.close()


def test_llm_router_local_success():
    """Valida que resposta bem-sucedida do PolyStrata local evita chamada ao Gemini."""
    mock_response_data = {
        "choices": [
            {
                "message": {
                    "content": json.dumps({
                        "lexical_density": 1.45,
                        "is_foreign_language": False,
                        "group_required": True,
                        "production_time_min": 60,
                        "reasoning_summary": "Artigo denso de RI sobre soberania monetária com seminário em grupo.",
                    })
                }
            }
        ]
    }

    mock_resp = httpx.Response(200, json=mock_response_data)
    with patch("httpx.Client.post", return_value=mock_resp):
        res = evaluate_task_with_local_llm("Seminário RI", "Texto das duas primeiras páginas...")
        assert res is not None
        assert res.lexical_density == 1.45
        assert res.group_required is True
        assert res.production_time_min == 60


def test_llm_router_fallback_to_gemini():
    """Valida que erro/timeout no endpoint local aciona automaticamente o fallback para o Gemini."""
    fallback_eval = SemanticTaskEvaluation(
        lexical_density=1.2,
        is_foreign_language=False,
        group_required=False,
        production_time_min=30,
        reasoning_summary="Fallback executado com sucesso.",
    )

    with patch("httpx.Client.post", side_effect=httpx.ConnectTimeout("Bazzite desligado")):
        with patch("backend.app.services.llm_router.evaluate_task_with_gemini_fallback", return_value=fallback_eval) as mock_cloud:
            res = evaluate_task_dual_router("Exercício Economia", "Texto...")
            assert res.lexical_density == 1.2
            mock_cloud.assert_called_once()


def test_mcp_preflight_head():
    """Valida que o probe HEAD /mcp retorna 200 OK com application/json para o Hermes Agent."""
    with TestClient(app) as client:
        resp = client.head("/mcp")
        assert resp.status_code == 200
        assert "application/json" in resp.headers.get("content-type", "")


def test_mcp_tools_crud(mock_db):
    """Valida as ferramentas MCP integradas com o SQLite."""
    # 1. Cadastra dados no banco mock
    course = Course(id="c1", name="Teoria das Relações Internacionais II", is_active=True)
    task = Task(
        id="t1",
        course_id="c1",
        title="Fichamento Waltz 1979",
        due_datetime=datetime.now(timezone.utc) + timedelta(days=3),
        est_duration_min=120,
        group_required=False,
        status="PENDING",
    )
    routine = DomesticRoutine(id=1, name="Limpar Filtro do AC", cron_expression="0 10 * * 1")
    mock_db.add_all([course, task, routine])
    mock_db.commit()

    # 2. Testa listar_tarefas_pendentes_unesp
    tasks_json = listar_tarefas_pendentes_unesp(dias_horizonte=7)
    tasks_data = json.loads(tasks_json)
    assert tasks_data["total"] == 1
    assert tasks_data["tarefas_pendentes"][0]["task_id"] == "t1"
    assert "Waltz" in tasks_data["tarefas_pendentes"][0]["titulo"]

    # 3. Testa consultar_jornada_diaria
    today_iso = datetime.now(timezone.utc).date().isoformat()
    jornada_json = consultar_jornada_diaria(today_iso)
    jornada_data = json.loads(jornada_json)
    assert len(jornada_data["rotinas_domesticas"]) == 1
    assert jornada_data["rotinas_domesticas"][0]["nome"] == "Limpar Filtro do AC"

    # 4. Testa concluir_tarefa_ou_rotina para task
    res_task = json.loads(concluir_tarefa_ou_rotina("task", "t1"))
    assert res_task["status"] == "success"
    task_reloaded = mock_db.query(Task).filter(Task.id == "t1").first()
    assert task_reloaded.status == "COMPLETED"

    # 5. Testa concluir_tarefa_ou_rotina para routine
    res_routine = json.loads(concluir_tarefa_ou_rotina("routine", "1"))
    assert res_routine["status"] == "success"
    routine_reloaded = mock_db.query(DomesticRoutine).filter(DomesticRoutine.id == 1).first()
    assert routine_reloaded.last_completed is not None


def test_mcp_ach_ri_matrix_deterministic():
    """Valida o motor determinístico local de ACH Popperiano para Relações Internacionais."""
    hipoteses = [
        "H1: Manobra de Dissuasão Convencional no Estreito de Taiwan",
        "H2: Preparação Iminente para Invasão Anfíbia",
    ]
    evidencias = [
        "E1: Exercício militar anunciado publicamente com data de término.",
        "E2: Tropas de logística e bancos de sangue não foram mobilizados.",
    ]

    ach_result = json.loads(executar_matriz_ach_ri(hipoteses, evidencias))
    assert ach_result["total_hipoteses"] == 2
    assert "probabilidades_posteriori" in ach_result
    assert ach_result["hipotese_mais_provavel"] in hipoteses
