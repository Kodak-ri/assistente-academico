from typing import Optional
from datetime import datetime
from pydantic import BaseModel, Field, field_validator, ConfigDict


class SemanticTaskEvaluation(BaseModel):
    """Contrato de saída estruturada para a avaliação semântica via Gemini."""

    lexical_density: float = Field(
        default=1.0,
        description="Fator de complexidade textual kappa_densidade no intervalo [1.0, 2.2]"
    )
    is_foreign_language: bool = Field(
        default=False,
        description="Verdadeiro se o texto não estiver no idioma materno português"
    )
    group_required: bool = Field(
        default=False,
        description="Verdadeiro se o enunciado ou aviso mencionar trabalho em grupo, equipa, dupla ou divisão de seminário"
    )
    production_time_min: int = Field(
        default=0,
        ge=0,
        description="Tempo adicional estimado em minutos T_producao para fichamento, relatório, exercícios ou redação"
    )
    reasoning_summary: str = Field(
        default="",
        description="Breve justificação da análise"
    )

    @field_validator("lexical_density", mode="before")
    @classmethod
    def clamp_density(cls, v: float) -> float:
        """Aplica o clamp numérico determinístico garantindo kappa_densidade em [1.0, 2.2]."""
        try:
            val = float(v)
            if val < 1.0:
                return 1.0
            if val > 2.2:
                return 2.2
            return round(val, 2)
        except (ValueError, TypeError):
            return 1.0


class TaskEffortCalculation(BaseModel):
    """Resultado detalhado do cálculo de esforço e leitura."""

    page_count: int
    lexical_density: float
    is_foreign_language: bool
    group_required: bool
    production_time_min: int
    reading_time_min: int
    total_duration_min: int
    reasoning_summary: str


class CourseSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    is_active: bool = True
    last_synced_at: Optional[datetime] = None


class TaskSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    course_id: str
    title: str
    due_datetime: datetime
    est_duration_min: int
    group_required: bool = False
    status: str = "PENDING"


class MaterialSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    course_id: str
    title: str
    page_count: int = 0
    lexical_density: float = 1.0
