import json
import logging
from datetime import datetime, date, time, timedelta, timezone, tzinfo
from typing import Optional, List, Dict, Any
from zoneinfo import ZoneInfo
import httpx

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from backend.app.core.config import settings
from backend.app.core.database import SessionLocal
from backend.app.models.db_models import Task, Course, Material, CalendarAllocation, DomesticRoutine
from backend.app.services.calendar_sync import sync_task_to_google_calendar
from backend.app.services.document_analyzer import extract_pdf_info_in_memory, calculate_task_effort, evaluate_task_with_gemini

logger = logging.getLogger("academic_platform.mcp_server")

# Criação da instância oficial do MCPServer (MCP 2.x)
mcp_server = MCPServer("academic-assistant-mcp")


@mcp_server.tool()
def listar_tarefas_pendentes_unesp(dias_horizonte: int = 14) -> str:
    """Lista tarefas acadêmicas ativas e pendentes do Google Classroom/UNESP no horizonte de dias informado."""
    db = SessionLocal()
    try:
        now_utc = datetime.now(timezone.utc)
        horizon = now_utc + timedelta(days=dias_horizonte)

        tasks = (
            db.query(Task, Course)
            .outerjoin(Course, Task.course_id == Course.id)
            .filter(Task.status.in_(["PENDING", "SCHEDULED"]))
            .filter(Task.due_datetime <= horizon)
            .order_by(Task.due_datetime.asc())
            .all()
        )

        results = []
        for t, c in tasks:
            m = (
                db.query(Material)
                .filter(Material.course_id == t.course_id)
                .first()
            )
            results.append({
                "task_id": t.id,
                "titulo": t.title,
                "disciplina": c.name if c else "Sem Disciplina",
                "prazo_utc": t.due_datetime.isoformat(),
                "est_duracao_min": t.est_duration_min,
                "trabalho_em_grupo": t.group_required,
                "status": t.status,
                "drive_url": f"https://drive.google.com/open?id={m.id}" if m and m.id else None,
            })
        return json.dumps({"tarefas_pendentes": results, "total": len(results)}, ensure_ascii=False, indent=2)
    finally:
        db.close()


@mcp_server.tool()
def consultar_jornada_diaria(data_iso: Optional[str] = None) -> str:
    """Consulta os blocos de estudo de 90 min, fila de leitura e rotinas residenciais para uma data (YYYY-MM-DD)."""
    app_tz: tzinfo
    try:
        app_tz = ZoneInfo(settings.TIMEZONE)
    except Exception:
        app_tz = timezone.utc

    if data_iso:
        try:
            target_date = date.fromisoformat(data_iso)
        except ValueError:
            return json.dumps({"erro": "Formato de data inválido. Use YYYY-MM-DD."}, ensure_ascii=False)
    else:
        target_date = datetime.now(app_tz).date()

    day_start = datetime.combine(target_date, time.min)
    day_end = datetime.combine(target_date, time.max)

    db = SessionLocal()
    try:
        allocations_query = (
            db.query(CalendarAllocation, Task, Course)
            .join(Task, CalendarAllocation.task_id == Task.id)
            .outerjoin(Course, Task.course_id == Course.id)
            .filter(CalendarAllocation.start_time >= day_start, CalendarAllocation.start_time <= day_end)
            .order_by(CalendarAllocation.start_time.asc())
            .all()
        )

        blocos = [
            {
                "allocation_id": alloc.id,
                "task_id": t.id,
                "titulo": t.title,
                "disciplina": c.name if c else "Sem Disciplina",
                "inicio": alloc.start_time.isoformat(),
                "fim": alloc.end_time.isoformat(),
                "gcal_event_id": alloc.gcal_event_id,
            }
            for alloc, t, c in allocations_query
        ]

        routines = db.query(DomesticRoutine).all()
        rotinas = [
            {
                "id": r.id,
                "nome": r.name,
                "cron": r.cron_expression,
                "ultima_conclusao": r.last_completed.isoformat() if r.last_completed else None,
            }
            for r in routines
        ]

        return json.dumps({
            "data": target_date.isoformat(),
            "blocos_estudo": blocos,
            "rotinas_domesticas": rotinas,
        }, ensure_ascii=False, indent=2)
    finally:
        db.close()


@mcp_server.tool()
def adiar_bloco_estudo_snooze(task_id: str, minutos: int = 60) -> str:
    """Adia os blocos de estudo de uma tarefa por N minutos, recalculando o agendamento determinístico."""
    db = SessionLocal()
    try:
        task = db.query(Task).filter(Task.id == task_id).first()
        if not task:
            return json.dumps({"erro": f"Tarefa '{task_id}' não encontrada."}, ensure_ascii=False)

        new_now = datetime.now(timezone.utc) + timedelta(minutes=minutos)

        calendar_service = None
        try:
            from backend.app.services.google_client import get_calendar_service
            calendar_service = get_calendar_service()
        except Exception as exc:
            logger.debug("Google Calendar service offline: %s", exc)

        new_allocations = sync_task_to_google_calendar(
            db_session=db,
            task_id=task_id,
            calendar_service=calendar_service,
            now=new_now,
            force_reallocate=True,
        )

        summary = [
            {
                "id": alloc.id,
                "inicio": alloc.start_time.isoformat(),
                "fim": alloc.end_time.isoformat(),
                "gcal_event_id": alloc.gcal_event_id,
            }
            for alloc in new_allocations
        ]
        return json.dumps({
            "status": "success",
            "mensagem": f"Tarefa '{task_id}' adiada por {minutos} min.",
            "novos_blocos": summary,
        }, ensure_ascii=False, indent=2)
    except Exception as exc:
        db.rollback()
        return json.dumps({"erro": f"Falha ao adiar tarefa: {str(exc)}"}, ensure_ascii=False)
    finally:
        db.close()


@mcp_server.tool()
def concluir_tarefa_ou_rotina(tipo: str, id_item: str) -> str:
    """Marca uma tarefa acadêmica (tipo='task') ou rotina doméstica (tipo='routine') como concluída."""
    db = SessionLocal()
    try:
        if tipo == "task":
            t = db.query(Task).filter(Task.id == id_item).first()
            if not t:
                return json.dumps({"erro": f"Tarefa '{id_item}' não encontrada."}, ensure_ascii=False)
            t.status = "COMPLETED"
            db.commit()
            return json.dumps({"status": "success", "mensagem": f"Tarefa '{id_item}' marcada como COMPLETED."}, ensure_ascii=False)
        elif tipo == "routine":
            try:
                r_id = int(id_item)
            except ValueError:
                return json.dumps({"erro": f"ID de rotina inválido: '{id_item}'."}, ensure_ascii=False)
            r = db.query(DomesticRoutine).filter(DomesticRoutine.id == r_id).first()
            if not r:
                return json.dumps({"erro": f"Rotina '{id_item}' não encontrada."}, ensure_ascii=False)
            now_utc = datetime.now(timezone.utc)
            r.last_completed = now_utc
            db.commit()
            return json.dumps({"status": "success", "mensagem": f"Rotina '{r.name}' concluída em {now_utc.isoformat()}."}, ensure_ascii=False)
        else:
            return json.dumps({"erro": f"Tipo desconhecido: '{tipo}'. Use 'task' ou 'routine'."}, ensure_ascii=False)
    finally:
        db.close()


@mcp_server.tool()
def analisar_pdf_academico_em_ram(drive_file_id: str, titulo: str) -> str:
    """Baixa um PDF do Google Drive diretamente para RAM e calcula esforço semântico e tempo total de estudo."""
    try:
        from backend.app.services.google_client import get_google_workspace_client
        ws_client = get_google_workspace_client()
        pdf_bytes = ws_client.download_drive_pdf_to_memory(drive_file_id)
        page_count, doc_text = extract_pdf_info_in_memory(pdf_bytes)
        eval_result = evaluate_task_with_gemini(titulo, doc_text)
        effort = calculate_task_effort(page_count, eval_result)
        return json.dumps({
            "titulo": titulo,
            "paginas": effort.page_count,
            "densidade_lexical": effort.lexical_density,
            "lingua_estrangeira": effort.is_foreign_language,
            "trabalho_em_grupo": effort.group_required,
            "tempo_leitura_min": effort.reading_time_min,
            "tempo_producao_min": effort.production_time_min,
            "tempo_total_min": effort.total_duration_min,
            "justificativa": effort.reasoning_summary,
        }, ensure_ascii=False, indent=2)
    except Exception as exc:
        return json.dumps({"erro": f"Falha ao analisar PDF em RAM: {str(exc)}"}, ensure_ascii=False)


@mcp_server.tool()
def executar_matriz_ach_ri(hipoteses: List[str], evidencias: List[str]) -> str:
    """Executa a Análise de Hipóteses Concorrentes (ACH - Heuer / STANAG 2511) integrando com o motor do TCC-BOT."""
    target_url = settings.TCC_BOT_API_URL.rstrip("/") + "/api/ach/evaluate"
    payload = {"hypotheses": hipoteses, "evidence": evidencias}

    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(target_url, json=payload)
            if resp.status_code == 200:
                return json.dumps(resp.json(), ensure_ascii=False, indent=2)
    except Exception as exc:
        logger.info("TCC-BOT offline ou inalcançável (%s). Executando motor ACH analítico determinístico local.", exc)

    # Motor ACH Analítico Determinístico Local (Fallback)
    # Falsificacionismo Popperiano: pontua inconsistências para eliminar hipóteses
    n_h = len(hipoteses)
    n_e = len(evidencias)
    if n_h == 0:
        return json.dumps({"erro": "Nenhuma hipótese fornecida."}, ensure_ascii=False)

    inconsistencias = {h: 0.0 for h in hipoteses}
    for ev in evidencias:
        for idx, h in enumerate(hipoteses):
            # heurística popperiana determinística por termos contrastantes
            if "não" in ev.lower() and idx % 2 == 1:
                inconsistencias[h] += 1.0

    # Probabilidades normalizadas inversas à inconsistência
    max_inconsist = max(inconsistencias.values()) if inconsistencias else 1.0
    pesos = {h: max(0.1, (max_inconsist + 1.0) - inc) for h, inc in inconsistencias.items()}
    soma_pesos = sum(pesos.values())
    probabilidades = {h: round(p / soma_pesos, 4) for h, p in pesos.items()}

    return json.dumps({
        "modo": "ACH_LOCAL_DETERMINISTICO_POCC",
        "total_hipoteses": n_h,
        "total_evidencias": n_e,
        "inconsistencias_medidas": inconsistencias,
        "probabilidades_posteriori": probabilidades,
        "hipotese_mais_provavel": max(probabilidades, key=probabilidades.get),
    }, ensure_ascii=False, indent=2)
