import math
from datetime import datetime, date, time, timedelta, timezone
from dataclasses import dataclass
from typing import List, Tuple, Optional


class InsufficientScheduleWindowError(Exception):
    """Lançada quando a margem antivéspera de 24h já expirou ou é insuficiente em relação a now."""
    pass


class ScheduleCapacityExceededError(Exception):
    """Lançada quando a agenda livre não possui capacidade suficiente para alocar todos os blocos."""
    pass


@dataclass
class ScheduledBlock:
    task_id: str
    block_index: int       # 1-indexed (ex: 1 de k)
    total_blocks: int      # k
    start_time: datetime
    end_time: datetime
    duration_min: int


@dataclass
class SchedulePlan:
    task_id: str
    total_blocks: int
    duration_per_block: int
    blocks: List[ScheduledBlock]


def calculate_cognitive_chunks(est_duration_min: int) -> Tuple[int, int]:
    """Calcula o fracionamento cognitivo anti-estafa (Restrição 2):
    
    - Se est_duration_min <= 90: bloco único (k = 1, d_bloco = est_duration_min).
    - Se est_duration_min > 90: fracionar em k blocos limitados a B_max = 75 minutos:
        k = math.ceil(est_duration_min / 75)
        d_bloco = math.ceil(est_duration_min / k)
    """
    if est_duration_min <= 90:
        return 1, est_duration_min

    k = math.ceil(est_duration_min / 75.0)
    d_bloco = math.ceil(est_duration_min / float(k))
    return k, d_bloco


def merge_busy_intervals(
    intervals: List[Tuple[datetime, datetime]]
) -> List[Tuple[datetime, datetime]]:
    """Mescla intervalos ocupados sobrepostos ou adjacentes (Restrição 4)."""
    if not intervals:
        return []

    # Ordena pelo início
    sorted_intervals = sorted(intervals, key=lambda x: x[0])
    merged: List[Tuple[datetime, datetime]] = []

    current_start, current_end = sorted_intervals[0]

    for next_start, next_end in sorted_intervals[1:]:
        if next_start <= current_end:
            # Sobreposição ou adjacência: expande o término
            current_end = max(current_end, next_end)
        else:
            merged.append((current_start, current_end))
            current_start, current_end = next_start, next_end

    merged.append((current_start, current_end))
    return merged


def get_daily_allowed_windows(
    target_date: date,
    tz: timezone = timezone.utc,
) -> List[Tuple[datetime, datetime]]:
    """Gera as duas janelas diárias estritas permitidas (Restrição 3):
    - Manhã: 08:30 às 11:30
    - Tarde/Noite: 14:00 às 21:00
    """
    morning_start = datetime.combine(target_date, time(8, 30), tzinfo=tz)
    morning_end = datetime.combine(target_date, time(11, 30), tzinfo=tz)

    afternoon_start = datetime.combine(target_date, time(14, 0), tzinfo=tz)
    afternoon_end = datetime.combine(target_date, time(21, 0), tzinfo=tz)

    return [(morning_start, morning_end), (afternoon_start, afternoon_end)]


def subtract_busy_from_window(
    window: Tuple[datetime, datetime],
    merged_busy: List[Tuple[datetime, datetime]],
) -> List[Tuple[datetime, datetime]]:
    """Realiza a subtração conjuntista de intervalos ocupados dentro de uma janela [W_start, W_end].
    Retorna uma lista de sub-intervalos livres contínuos.
    """
    w_start, w_end = window
    if w_start >= w_end:
        return []

    free_segments: List[Tuple[datetime, datetime]] = []
    current_cursor = w_start

    for b_start, b_end in merged_busy:
        if b_end <= current_cursor:
            continue
        if b_start >= w_end:
            break

        # Caso o ocupado intercepte o cursor atual
        effective_busy_start = max(b_start, current_cursor)
        effective_busy_end = min(b_end, w_end)

        if effective_busy_start > current_cursor:
            free_segments.append((current_cursor, effective_busy_start))

        current_cursor = max(current_cursor, effective_busy_end)
        if current_cursor >= w_end:
            break

    if current_cursor < w_end:
        free_segments.append((current_cursor, w_end))

    return free_segments


class DeterministicSchedulerEngine:
    """Motor Determinístico de Agendamento Linear baseado em CSP com satisfação de 4 restrições estritas."""

    def __init__(self, buffer_min: int = 10):
        self.buffer_min = buffer_min  # Buffer de transição de 10 min em cada extremidade

    def compute_schedule(
        self,
        task_id: str,
        est_duration_min: int,
        due_datetime: datetime,
        now: Optional[datetime] = None,
        busy_intervals: Optional[List[Tuple[datetime, datetime]]] = None,
    ) -> SchedulePlan:
        """Calcula o cronograma linear determinístico aplicando as 4 restrições:
        
        1. Margem antivéspera de 24h: T_alvo_fim = due_datetime - 24h.
        2. Fracionamento cognitivo: sessões balanceadas limitadas a 75 min (se > 90 min).
        3. Janelas permitidas: 08:30-11:30 e 14:00-21:00.
        4. Subtração conjuntista e buffer de 10 min antes e 10 min depois de cada bloco.
        Priorização de distribuição: Dias intercalados -> 1 bloco/dia -> múltiplos blocos/dia.
        """
        # Normalização de timezone
        tz = due_datetime.tzinfo or timezone.utc
        ref_now = now or datetime.now(tz)
        if ref_now.tzinfo is None:
            ref_now = ref_now.replace(tzinfo=tz)

        # ---------------------------------------------------------------------
        # Restrição 1: Margem de Segurança Antivéspera (24h)
        # ---------------------------------------------------------------------
        target_deadline = due_datetime - timedelta(hours=24)
        if ref_now >= target_deadline:
            raise InsufficientScheduleWindowError(
                f"Janela de agendamento insuficiente para tarefa '{task_id}': "
                f"T_alvo_fim ({target_deadline.isoformat()}) é anterior ou igual a now ({ref_now.isoformat()})."
            )

        # ---------------------------------------------------------------------
        # Restrição 2: Fracionamento Cognitivo
        # ---------------------------------------------------------------------
        k_blocks, d_bloco = calculate_cognitive_chunks(est_duration_min)

        # Normaliza intervalos ocupados e realiza o merge
        normalized_busy: List[Tuple[datetime, datetime]] = []
        if busy_intervals:
            for b_s, b_e in busy_intervals:
                bs_norm = b_s if b_s.tzinfo else b_s.replace(tzinfo=tz)
                be_norm = b_e if b_e.tzinfo else b_e.replace(tzinfo=tz)
                normalized_busy.append((bs_norm, be_norm))

        merged_busy = merge_busy_intervals(normalized_busy)

        # ---------------------------------------------------------------------
        # Coleta de Janelas Livres por Dia
        # ---------------------------------------------------------------------
        # Gera os dias entre ref_now.date() e target_deadline.date()
        start_date = ref_now.date()
        end_date = target_deadline.date()
        num_days = (end_date - start_date).days + 1

        # Mapeia: day_date -> lista de segmentos livres
        daily_free_segments: List[Tuple[date, List[Tuple[datetime, datetime]]]] = []

        for day_offset in range(num_days):
            current_day = start_date + timedelta(days=day_offset)
            raw_windows = get_daily_allowed_windows(current_day, tz=tz)

            day_segments: List[Tuple[datetime, datetime]] = []
            for w_start, w_end in raw_windows:
                # Recorta na fronteira [ref_now, target_deadline]
                eff_w_start = max(w_start, ref_now)
                eff_w_end = min(w_end, target_deadline)

                if eff_w_start < eff_w_end:
                    free_segs = subtract_busy_from_window((eff_w_start, eff_w_end), merged_busy)
                    day_segments.extend(free_segs)

            daily_free_segments.append((current_day, day_segments))

        # ---------------------------------------------------------------------
        # Alocação com Priorização CSP:
        # Tier 1: Dias intercalados (pelo menos 1 dia de intervalo entre sessões)
        # Tier 2: No máximo 1 bloco por dia (dias consecutivos permitidos)
        # Tier 3: Múltiplos blocos no mesmo dia (em janelas distintas / capacidade remanescente)
        # ---------------------------------------------------------------------
        # Tentativa Tier 1 (Dias intercalados)
        if k_blocks > 1:
            plan_tier1 = self._try_allocate(
                task_id=task_id,
                k_blocks=k_blocks,
                d_bloco=d_bloco,
                daily_free_segments=daily_free_segments,
                min_day_stride=2,     # Intercalado: pula pelo menos 1 dia
                max_blocks_per_day=1,
            )
            if plan_tier1 is not None:
                return plan_tier1

        # Tentativa Tier 2 (No máximo 1 bloco por dia)
        plan_tier2 = self._try_allocate(
            task_id=task_id,
            k_blocks=k_blocks,
            d_bloco=d_bloco,
            daily_free_segments=daily_free_segments,
            min_day_stride=1,         # Dias consecutivos permitidos
            max_blocks_per_day=1,
        )
        if plan_tier2 is not None:
            return plan_tier2

        # Tentativa Tier 3 (Qualquer janela livre disponível até T_alvo_fim)
        plan_tier3 = self._try_allocate(
            task_id=task_id,
            k_blocks=k_blocks,
            d_bloco=d_bloco,
            daily_free_segments=daily_free_segments,
            min_day_stride=0,
            max_blocks_per_day=999,
        )
        if plan_tier3 is not None:
            return plan_tier3

        # Se todas as estratégias falharem por falta de janelas
        raise ScheduleCapacityExceededError(
            f"Capacidade de agenda excedida para tarefa '{task_id}'. "
            f"Não foi possível alocar todos os {k_blocks} blocos de {d_bloco} min "
            f"(com buffers de {self.buffer_min} min) antes do prazo {target_deadline.isoformat()}."
        )

    def _try_allocate(
        self,
        task_id: str,
        k_blocks: int,
        d_bloco: int,
        daily_free_segments: List[Tuple[date, List[Tuple[datetime, datetime]]]],
        min_day_stride: int,
        max_blocks_per_day: int,
    ) -> Optional[SchedulePlan]:
        """Tenta alocar deterministicamente k blocos respeitando as restrições do Tier fornecido."""
        required_slot_duration = d_bloco + (2 * self.buffer_min)  # d_bloco + 20 min
        allocated_blocks: List[ScheduledBlock] = []

        last_allocated_day_idx: Optional[int] = None

        # Cria cópia de trabalho dos segmentos livres para não contaminar outras tentativas
        working_segments = [
            (day_date, list(segs)) for day_date, segs in daily_free_segments
        ]

        for day_idx, (day_date, segs) in enumerate(working_segments):
            if len(allocated_blocks) >= k_blocks:
                break

            # Se houver restrição de stride de dias
            if last_allocated_day_idx is not None and min_day_stride > 0:
                if (day_idx - last_allocated_day_idx) < min_day_stride:
                    continue

            blocks_today = 0
            seg_idx = 0
            while seg_idx < len(segs) and len(allocated_blocks) < k_blocks and blocks_today < max_blocks_per_day:
                s_start, s_end = segs[seg_idx]
                available_min = (s_end - s_start).total_seconds() / 60.0

                if available_min >= required_slot_duration:
                    # Encontrou capacidade!
                    # O bloco começa exatamente após o buffer inicial de 10 min
                    block_start = s_start + timedelta(minutes=self.buffer_min)
                    block_end = block_start + timedelta(minutes=d_bloco)

                    block_obj = ScheduledBlock(
                        task_id=task_id,
                        block_index=len(allocated_blocks) + 1,
                        total_blocks=k_blocks,
                        start_time=block_start,
                        end_time=block_end,
                        duration_min=d_bloco,
                    )
                    allocated_blocks.append(block_obj)
                    blocks_today += 1
                    last_allocated_day_idx = day_idx

                    # O espaço consumido no segmento é até o fim do buffer posterior (block_end + 10 min)
                    next_available_start = block_end + timedelta(minutes=self.buffer_min)
                    if next_available_start < s_end:
                        # Atualiza o segmento atual com a capacidade restante
                        segs[seg_idx] = (next_available_start, s_end)
                    else:
                        seg_idx += 1
                else:
                    seg_idx += 1

        if len(allocated_blocks) == k_blocks:
            return SchedulePlan(
                task_id=task_id,
                total_blocks=k_blocks,
                duration_per_block=d_bloco,
                blocks=allocated_blocks,
            )

        return None
