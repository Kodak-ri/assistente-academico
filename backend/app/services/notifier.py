import asyncio
import logging
from datetime import datetime
from typing import Optional, List, Dict, Any

import httpx

from backend.app.core.config import settings

logger = logging.getLogger("academic_platform.notifier")


class NtfyNotifier:
    """Cliente assíncrono para despacho de notificações e ações interativas via ntfy.sh."""

    def __init__(
        self,
        server_url: Optional[str] = None,
        topic: Optional[str] = None,
        public_api_base_url: Optional[str] = None,
        client: Optional[httpx.AsyncClient] = None,
        timeout: float = 10.0,
        max_retries: int = 3,
        backoff_base: float = 0.5,
    ) -> None:
        self.server_url = (server_url or settings.NTFY_SERVER_URL).rstrip("/")
        self.topic = topic or settings.NTFY_TOPIC
        self.public_api_base_url = (public_api_base_url or settings.PUBLIC_API_BASE_URL).rstrip("/")
        self._injected_client = client
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base

    @property
    def target_url(self) -> str:
        return f"{self.server_url}/{self.topic}"

    async def send_notification(
        self,
        message: str,
        title: Optional[str] = None,
        priority: int = 5,
        tags: Optional[List[str]] = None,
        actions: Optional[List[str]] = None,
        click: Optional[str] = None,
    ) -> bool:
        """Envia uma notificação HTTP POST para o tópico ntfy com retry e exponential backoff.
        
        Garante timeout de 10s e resiliência a oscilações de rede sem quebrar o estado local.
        """
        raw_headers: Dict[str, str] = {
            "Priority": str(priority),
        }
        if title:
            raw_headers["Title"] = title
        if tags:
            raw_headers["Tags"] = ",".join(tags)
        if actions:
            # Protocolo ntfy separa múltiplas ações pelo delimitador ';'
            raw_headers["Actions"] = "; ".join(actions)
        if click:
            raw_headers["Click"] = click

        headers = httpx.Headers(raw_headers, encoding="utf-8")

        delay = self.backoff_base
        for attempt in range(1, self.max_retries + 1):
            try:
                if self._injected_client is not None:
                    response = await self._injected_client.post(
                        self.target_url,
                        content=message.encode("utf-8"),
                        headers=headers,
                        timeout=self.timeout,
                    )
                    response.raise_for_status()
                    return True
                else:
                    async with httpx.AsyncClient(timeout=self.timeout) as client:
                        response = await client.post(
                            self.target_url,
                            content=message.encode("utf-8"),
                            headers=headers,
                        )
                        response.raise_for_status()
                        return True
            except (httpx.RequestError, httpx.HTTPStatusError) as exc:
                logger.warning(
                    "Falha ao enviar notificação ntfy (tentativa %d/%d): %s",
                    attempt,
                    self.max_retries,
                    exc,
                )
                if attempt < self.max_retries:
                    await asyncio.sleep(delay)
                    delay *= 2
                else:
                    logger.error(
                        "Esgotadas %d tentativas de envio ao ntfy. Operação preservada localmente.",
                        self.max_retries,
                    )
                    return False
            except Exception as exc:
                logger.error("Erro inesperado ao despachar notificação ntfy: %s", exc)
                return False

        return False

    async def send_daily_briefing(
        self,
        date_str: str,
        study_blocks: Optional[List[Dict[str, Any]]] = None,
        reading_queue: Optional[List[Dict[str, Any]]] = None,
        domestic_routines: Optional[List[Dict[str, Any]]] = None,
    ) -> bool:
        """Dispara o despacho matinal das 07:30 com resumo do dia."""
        study_blocks = study_blocks or []
        reading_queue = reading_queue or []
        domestic_routines = domestic_routines or []

        msg_lines = [
            f"📅 Despacho Diário - {date_str}",
            "",
            f"📚 Sessões de Estudo Alocadas ({len(study_blocks)}):",
        ]
        if study_blocks:
            for blk in study_blocks:
                start = blk.get("start_time", "")
                title = blk.get("task_title", "Estudo")
                course = blk.get("course_name", "")
                msg_lines.append(f"  • [{start}] {course}: {title}")
        else:
            msg_lines.append("  • Nenhuma sessão agendada para hoje.")

        msg_lines.append("")
        msg_lines.append(f"📖 Fila de Leituras Pendentes ({len(reading_queue)}):")
        if reading_queue:
            for item in reading_queue[:5]:
                t_title = item.get("title", "")
                pages = item.get("page_count", 0)
                msg_lines.append(f"  • {t_title} ({pages} págs)")
        else:
            msg_lines.append("  • Nenhuma leitura pendente.")

        msg_lines.append("")
        msg_lines.append(f"🏠 Rotinas Domésticas ({len(domestic_routines)}):")
        if domestic_routines:
            for r in domestic_routines:
                r_name = r.get("name", "")
                r_status = "✅ Concluída" if r.get("is_completed_today") else "⏳ Pendente"
                msg_lines.append(f"  • {r_name} - {r_status}")
        else:
            msg_lines.append("  • Nenhuma rotina cadastrada.")

        dashboard_url = f"{self.public_api_base_url}/api/dashboard/daily"
        actions = [
            f"view, Abrir Painel, {dashboard_url}"
        ]

        return await self.send_notification(
            message="\n".join(msg_lines),
            title=f"Despacho Diário: {date_str}",
            priority=5,
            tags=["calendar", "clipboard", "sunrise"],
            actions=actions,
        )

    async def send_study_block_alert(
        self,
        task_id: str,
        course_name: str,
        task_title: str,
        block_index: int,
        total_blocks: int,
        duration_min: int,
        material_url: Optional[str] = None,
    ) -> bool:
        """Envia o alerta de início de bloco de estudo com os botões de ação interativos."""
        message = (
            f"Sessão de estudo iniciada: {task_title}\n"
            f"Disciplina: {course_name}\n"
            f"Progresso: Bloco {block_index} de {total_blocks} ({duration_min} minutos)."
        )

        actions = [
            f"http, Concluir, {self.public_api_base_url}/api/tasks/{task_id}/complete, method=POST, clear=true",
            f"http, Adiar 1h, {self.public_api_base_url}/api/tasks/{task_id}/snooze?min=60, method=POST, clear=true",
        ]
        if material_url:
            actions.append(f"view, Abrir PDF, {material_url}")

        return await self.send_notification(
            message=message,
            title=f"Bloco de Estudo: {course_name}",
            priority=5,
            tags=["books", "hourglass_flowing_sand"],
            actions=actions,
        )

    async def send_group_urgent_alert(
        self,
        task_id: str,
        task_title: str,
        course_name: str,
        due_datetime: Optional[datetime] = None,
        reasoning: Optional[str] = None,
    ) -> bool:
        """Alerta de prioridade máxima quando group_required=True for detectado na triagem."""
        deadline_str = due_datetime.strftime("%d/%m/%Y %H:%M") if due_datetime else "A definir"
        reasoning_text = f"\nJustificativa: {reasoning}" if reasoning else ""

        message = (
            f"Aviso Crítico: Foi detectada a exigência de trabalho em grupo/equipa!\n"
            f"Disciplina: {course_name}\n"
            f"Atividade: {task_title}\n"
            f"Prazo final: {deadline_str}{reasoning_text}\n"
            f"Ação imediata recomendada: alinhar divisão e prazos com os membros do grupo."
        )

        actions = [
            f"http, Concluir, {self.public_api_base_url}/api/tasks/{task_id}/complete, method=POST, clear=true"
        ]

        return await self.send_notification(
            message=message,
            title="Alerta Urgente: Trabalho em Grupo Detectado",
            priority=5,
            tags=["warning", "people_hugging", "rotating_light"],
            actions=actions,
        )

    async def send_domestic_routine_alert(
        self,
        routine_id: int,
        routine_name: str,
    ) -> bool:
        """Envia lembrete de tarefa residencial com botão de confirmação."""
        message = (
            f"Lembrete de rotina residencial: {routine_name}.\n"
            f"Toque em Concluir após finalizar para manter seu ciclo atualizado."
        )

        actions = [
            f"http, Concluir, {self.public_api_base_url}/api/routines/{routine_id}/complete, method=POST, clear=true"
        ]

        return await self.send_notification(
            message=message,
            title=f"Rotina Doméstica: {routine_name}",
            priority=5,
            tags=["house", "sparkles"],
            actions=actions,
        )

    async def send_oauth_emergency_alert(
        self,
        error_details: str,
    ) -> bool:
        """Alerta crítico caso a renovação do refresh_token OAuth2 falhe."""
        message = (
            f"FALHA CRÍTICA DE AUTENTICAÇÃO GOOGLE WORKSPACE!\n"
            f"Detalhes do erro: {error_details}\n"
            f"O refresh_token expirou ou foi revogado. O agendamento e sincronização "
            f"estão pausados até a renovação manual das credenciais."
        )

        return await self.send_notification(
            message=message,
            title="Emergência OAuth2: Falha de Renovação de Token",
            priority=5,
            tags=["key", "skull", "warning"],
        )


# Instância padrão para conveniência
default_notifier = NtfyNotifier()
