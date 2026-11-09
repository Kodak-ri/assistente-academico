import json
import logging
import os
import re
from typing import Any, Optional
import httpx

from backend.app.core.config import settings
from backend.app.models.schemas import SemanticTaskEvaluation

logger = logging.getLogger("academic_platform.llm_router")


def _clean_json_markdown(text: str) -> str:
    """Extrai JSON bruto de blocos com marcação markdown se existirem."""
    text = text.strip()
    match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
    if match:
        return match.group(1).strip()
    return text


def evaluate_task_with_local_llm(
    task_title: str,
    document_text: str,
    base_url: Optional[str] = None,
    model: Optional[str] = None,
    timeout_sec: Optional[float] = None,
) -> Optional[SemanticTaskEvaluation]:
    """Tenta avaliar o esforço da tarefa via endpoint local OpenAI-compatible (PolyStrata/Bazzite).
    
    Retorna SemanticTaskEvaluation em caso de sucesso ou None em caso de falha de conexão/timeout.
    """
    url = (base_url or settings.LOCAL_LLM_BASE_URL).rstrip("/") + "/chat/completions"
    target_model = model or settings.LOCAL_LLM_MODEL
    timeout = timeout_sec if timeout_sec is not None else settings.LOCAL_LLM_TIMEOUT_SEC

    prompt = (
        f"Você é um analisador semântico de esforço acadêmico.\n"
        f"Analise o título da tarefa e o extrato do documento associado (primeiras 2 páginas).\n"
        f"Identifique:\n"
        f"1. lexical_density: complexidade e densidade técnica do texto (fator float entre 1.0 e 2.2).\n"
        f"2. is_foreign_language: booleano (True se o texto principal for inglês ou outro idioma estrangeiro).\n"
        f"3. group_required: booleano (True se mencionar explicitamente trabalho em grupo, equipe, dupla ou divisão de seminário).\n"
        f"4. production_time_min: tempo estimado em minutos para resolver exercícios, escrever código ou produzir relatório (0 se for apenas leitura).\n"
        f"5. reasoning_summary: breve justificativa da análise.\n\n"
        f"Título da Tarefa: {task_title}\n\n"
        f"Extrato do Documento:\n{document_text}\n"
    )

    payload = {
        "model": target_model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Você é um analisador semântico de esforço acadêmico. "
                    "Responda estritamente com um objeto JSON válido (sem explicações adicionais) "
                    "com as seguintes chaves:\n"
                    '{\n'
                    '  "lexical_density": 1.0 a 2.2,\n'
                    '  "is_foreign_language": false,\n'
                    '  "group_required": false,\n'
                    '  "production_time_min": 0,\n'
                    '  "reasoning_summary": "resumo"\n'
                    '}'
                ),
            },
            {
                "role": "user",
                "content": prompt,
            },
        ],
        "temperature": 0.1,
    }

    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(url, json=payload)
            if resp.status_code == 200:
                data = resp.json()
                content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                clean_json = _clean_json_markdown(content)
                parsed = json.loads(clean_json)
                evaluation = SemanticTaskEvaluation.model_validate(parsed)
                logger.info(
                    "Avaliação semântica concluída com sucesso via Local LLM (%s na porta 11434).",
                    target_model,
                )
                return evaluation
            else:
                logger.warning(
                    "Local LLM retornou status HTTP %d: %s. Acionando fallback Gemini.",
                    resp.status_code,
                    resp.text[:150],
                )
                return None
    except Exception as exc:
        logger.info(
            "Local LLM indisponível ou timeout (%s). Acionando fallback Gemini 3.8 Flash. Erro: %s",
            url,
            exc,
        )
        return None


def evaluate_task_with_gemini_fallback(
    task_title: str,
    document_text: str,
    gemini_client: Any = None,
) -> SemanticTaskEvaluation:
    """Executa a avaliação semântica via SDK google-genai com Gemini 3.8 Flash (Fallback de Alta Disponibilidade)."""
    if gemini_client is None:
        from google import genai
        api_key = settings.GEMINI_API_KEY or os.getenv("GEMINI_API_KEY")
        gemini_client = genai.Client(api_key=api_key)

    prompt = (
        f"Você é um analisador semântico de esforço académico.\n"
        f"Analise o título da tarefa e o extrato do documento associado (primeiras 2 páginas).\n"
        f"Identifique:\n"
        f"1. lexical_density: complexidade e densidade técnica do texto (fator float entre 1.0 e 2.2).\n"
        f"2. is_foreign_language: booleano (True se o texto principal for inglês ou outro idioma estrangeiro).\n"
        f"3. group_required: booleano (True se mencionar explicitamente trabalho em grupo, equipe, dupla ou divisão de seminário).\n"
        f"4. production_time_min: tempo estimado em minutos para resolver exercícios, escrever código ou produzir relatório (0 se for apenas leitura).\n"
        f"5. reasoning_summary: breve justificativa da análise.\n\n"
        f"Título da Tarefa: {task_title}\n\n"
        f"Extrato do Documento:\n{document_text}\n"
    )

    from google.genai import types
    response = gemini_client.models.generate_content(
        model=settings.GEMINI_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=SemanticTaskEvaluation,
        ),
    )

    if hasattr(response, "parsed") and isinstance(response.parsed, SemanticTaskEvaluation):
        return response.parsed

    raw_text = response.text if hasattr(response, "text") else str(response)
    data = json.loads(raw_text)
    return SemanticTaskEvaluation.model_validate(data)


def evaluate_task_dual_router(
    task_title: str,
    document_text: str,
    gemini_client: Any = None,
    force_local: bool = False,
    force_cloud: bool = False,
) -> SemanticTaskEvaluation:
    """Roteador Dual:
    1. Se gemini_client for fornecido explicitamente (ex: mocks de teste) ou force_cloud=True:
       usa diretamente o provedor Gemini.
    2. Caso contrário:
       tenta primeiro a GPU local via PolyStrata (:11434) com timeout curto.
       Se falhar e force_local=False, executa fallback automático para Gemini Cloud.
    """
    if gemini_client is not None or force_cloud:
        return evaluate_task_with_gemini_fallback(task_title, document_text, gemini_client=gemini_client)

    local_eval = evaluate_task_with_local_llm(task_title, document_text)
    if local_eval is not None:
        return local_eval

    if force_local:
        raise RuntimeError("Local LLM falhou e force_local está ativo.")

    logger.info("Executando fallback para o provedor Gemini Cloud.")
    return evaluate_task_with_gemini_fallback(task_title, document_text, gemini_client=gemini_client)
