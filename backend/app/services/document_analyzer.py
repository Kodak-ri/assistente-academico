import asyncio
import inspect
import io
import json
import logging
import math
import os
from datetime import datetime, timezone
from typing import Union, Tuple, Optional, Callable, Any

import pymupdf
from sqlalchemy.orm import Session

from backend.app.core.config import settings
from backend.app.models.db_models import Course, Material, Task
from backend.app.models.schemas import SemanticTaskEvaluation, TaskEffortCalculation

logger = logging.getLogger("academic_platform.document_analyzer")

# Hook global configurável para alertas prioritários (ntfy/webhooks)
_priority_alert_hook: Optional[Callable[[str, str], Any]] = None


def default_priority_alert_hook(task_title: str, reason: str) -> None:
    """Callback padrão de log para alertas prioritários de trabalho em grupo."""
    logger.warning(f"[ALERTA PRIORITÁRIO - TRABALHO EM GRUPO] Tarefa: '{task_title}' | Detalhes: {reason}")


def set_priority_alert_hook(hook: Optional[Callable[[str, str], Any]]) -> None:
    """Define um hook customizado para disparos de alerta prioritário."""
    global _priority_alert_hook
    _priority_alert_hook = hook


def get_priority_alert_hook() -> Callable[[str, str], Any]:
    """Retorna o hook configurado ou o callback padrão."""
    return _priority_alert_hook or default_priority_alert_hook


def _dispatch_coro_safely(coro: Any) -> None:
    """Executa ou agenda uma corrotina de forma segura a partir de contexto síncrono ou assíncrono."""
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(coro)
    except RuntimeError:
        asyncio.run(coro)


def extract_pdf_info_in_memory(pdf_source: Union[bytes, io.BytesIO]) -> Tuple[int, str]:
    """Lê o PDF estritamente em memória usando PyMuPDF (fitz), sem escrita em disco.
    
    Retorna (N_paginas, texto_2_primeiras_paginas).
    """
    if isinstance(pdf_source, io.BytesIO):
        pdf_bytes = pdf_source.getvalue()
    elif isinstance(pdf_source, bytes):
        pdf_bytes = pdf_source
    else:
        raise ValueError("pdf_source deve ser bytes ou io.BytesIO")

    extracted_pages = []
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        page_count = len(doc)
        # Extrai o texto integral das duas primeiras páginas
        for page_idx in range(min(2, page_count)):
            page_text = doc[page_idx].get_text()
            if page_text:
                extracted_pages.append(page_text.strip())

    combined_text = "\n\n--- PÁGINA SEGUINTE ---\n\n".join(extracted_pages)
    return page_count, combined_text


def calculate_task_effort(
    page_count: int,
    evaluation: SemanticTaskEvaluation,
) -> TaskEffortCalculation:
    """Calcula o tempo total estimado usando a fórmula matemática especificada na SPEC-001:
    
    t_base = 3.5 minutos por página
    gamma_lang = 1.4 se is_foreign_language == True senão 1.0
    lexical_density em [1.0, 2.2]
    T_leitura = math.ceil(P_total * 3.5 * lexical_density * gamma_lang)
    T_total = T_leitura + T_prod
    """
    t_base = 3.5

    # Clamp numérico determinístico sobre lexical_density em [1.0, 2.2]
    raw_kappa = float(evaluation.lexical_density)
    kappa_densidade = max(1.0, min(2.2, raw_kappa))

    # Penalização de língua estrangeira (gamma_lang)
    gamma_lang = 1.4 if evaluation.is_foreign_language else 1.0

    # Cálculo estrito do tempo de leitura com teto inteiro (math.ceil)
    t_leitura = math.ceil(page_count * t_base * kappa_densidade * gamma_lang)

    # Tempo total
    t_total = t_leitura + int(evaluation.production_time_min)

    return TaskEffortCalculation(
        page_count=page_count,
        lexical_density=round(kappa_densidade, 2),
        is_foreign_language=evaluation.is_foreign_language,
        group_required=evaluation.group_required,
        production_time_min=evaluation.production_time_min,
        reading_time_min=t_leitura,
        total_duration_min=t_total,
        reasoning_summary=evaluation.reasoning_summary,
    )


def evaluate_task_with_gemini(
    task_title: str,
    document_text: str,
    gemini_client: Any = None,
) -> SemanticTaskEvaluation:
    """Avalia o esforço semântico utilizando o Roteador Dual (Local Bazzite GPU -> Fallback Gemini Cloud).
    
    1. Tenta primeiro o modelo local no Bazzite (Qwen3.6-Q2) na porta 11434 (custo zero).
    2. Se Bazzite estiver indisponível ou em modo jogo, faz fallback imediato para Gemini 3.8 Flash.
    3. Se gemini_client for passado explicitamente (ex: mocks de teste), preserva chamada direta.
    """
    from backend.app.services.llm_router import evaluate_task_dual_router
    return evaluate_task_dual_router(task_title, document_text, gemini_client=gemini_client)


def process_document_and_persist(
    pdf_source: Union[bytes, io.BytesIO],
    course_id: str,
    course_name: str,
    task_id: str,
    task_title: str,
    due_datetime: datetime,
    file_id: str,
    db_session: Session,
    gemini_client: Any = None,
    alert_hook: Optional[Callable[[str, str], Any]] = None,
    custom_evaluation: Optional[SemanticTaskEvaluation] = None,
    notifier: Any = None,
) -> TaskEffortCalculation:
    """Fluxo unificado:
    1. Leitura binária em memória do PDF com PyMuPDF.
    2. Avaliação semântica via Gemini 2.5 Flash (ou avaliação pré-configurada).
    3. Cálculo matemático de esforço T_leitura e T_total.
    4. Disparo do alerta prioritário se group_required == True.
    5. Persistência relacional em courses, materials e tasks no SQLite.
    """
    page_count, doc_text = extract_pdf_info_in_memory(pdf_source)

    if custom_evaluation:
        evaluation = custom_evaluation
    else:
        evaluation = evaluate_task_with_gemini(task_title, doc_text, gemini_client=gemini_client)

    effort = calculate_task_effort(page_count, evaluation)

    # Disparo do alerta prioritário se group_required for verdadeiro
    if effort.group_required:
        hook_to_call = alert_hook or get_priority_alert_hook()
        hook_res = hook_to_call(task_title, effort.reasoning_summary)
        if inspect.isawaitable(hook_res):
            _dispatch_coro_safely(hook_res)
        if notifier is not None:
            coro = notifier.send_group_urgent_alert(
                task_id=task_id,
                task_title=task_title,
                course_name=course_name,
                due_datetime=due_datetime,
                reasoning=effort.reasoning_summary,
            )
            _dispatch_coro_safely(coro)

    # Persistência relacional
    # 1. Course
    course = db_session.query(Course).filter(Course.id == course_id).first()
    now_utc = datetime.now(timezone.utc)
    if not course:
        course = Course(
            id=course_id,
            name=course_name,
            is_active=True,
            last_synced_at=now_utc,
        )
        db_session.add(course)
    else:
        course.is_active = True
        course.last_synced_at = now_utc

    # 2. Material
    material = db_session.query(Material).filter(Material.id == file_id).first()
    if not material:
        material = Material(
            id=file_id,
            course_id=course_id,
            title=task_title,
            page_count=effort.page_count,
            lexical_density=effort.lexical_density,
        )
        db_session.add(material)
    else:
        material.title = task_title
        material.page_count = effort.page_count
        material.lexical_density = effort.lexical_density

    # 3. Task
    task = db_session.query(Task).filter(Task.id == task_id).first()
    if not task:
        task = Task(
            id=task_id,
            course_id=course_id,
            title=task_title,
            due_datetime=due_datetime,
            est_duration_min=effort.total_duration_min,
            group_required=effort.group_required,
            status="PENDING",
        )
        db_session.add(task)
    else:
        task.title = task_title
        task.due_datetime = due_datetime
        task.est_duration_min = effort.total_duration_min
        task.group_required = effort.group_required

    db_session.commit()
    return effort
