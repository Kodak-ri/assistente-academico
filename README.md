# 🎓 Assistente Acadêmico Autônomo & Agendamento Preditivo

Plataforma autônoma headless para gestão acadêmica, triagem inteligente de materiais do Google Workspace (Classroom, Drive, Calendar) e estimativa semântica de esforço temporal com IA (Gemini).

---

## 🚀 1. Visão Geral e Objetivo

O **Assistente Acadêmico Autônomo** é projetado para eliminar a sobrecarga de organização acadêmica de estudantes universitários. Ele atua como um agente em segundo plano que:

1. **Sincroniza e filtra** as turmas ativas do semestre letivo no Google Classroom.
2. **Identifica anexos PDF** de materiais de aula e enunciados de tarefas diretamente no Google Drive.
3. **Extrai texto em memória** (sem tocar o disco) com PyMuPDF.
4. **Avalia a complexidade cognitiva** e requisitos de produção via Gemini API (`gemini-2.5-flash`) com Structured Outputs.
5. **Calcula o tempo de dedicação real** com fórmulas calibradas e regras de negócio determinísticas.
6. **Emite alertas imediatos** para tarefas que exigem trabalho em grupo, divisão de seminários ou equipes.
7. **Persiste todo o ecossistema** de forma relacional em SQLite via SQLAlchemy 2.0.

---

## 🏗️ 2. Arquitetura da Solução (Fase 1 — SPEC-001)

```mermaid
flowchart TD
    subgraph Ingestao["1. Ingestão & Triagem Tripartida"]
        GC[Google Classroom API] -->|courses.list| F1[Filtro 1: Criação < 180 dias]
        F1 --> F2[Filtro 2: Persistência Local is_active != False]
        F2 --> F3[Filtro 3: Atividade Mural/Tarefas < 45 dias]
        F3 -->|Turmas Vigentes| GD[Google Drive API]
    end

    subgraph Processamento["2. Processamento em Memória"]
        GD -->|drive.files.get_media| RAM[io.BytesIO - Sem escrita em disco]
        RAM --> FITZ[PyMuPDF: Extração de Páginas e Amostra Textual]
        FITZ --> GENAI[Gemini 2.5 Flash: Structured Outputs]
    end

    subgraph Calculo["3. Cálculo de Esforço & Decisão"]
        GENAI --> FORM[Fórmula de Esforço Temporal]
        FORM -->|group_required == True| HOOK[Hook Alerta Prioritário]
        FORM --> DB[(SQLite: SQLAlchemy 2.0)]
    end
```

### Componentes Principais

- **Persistência (`backend/app/core/` e `backend/app/models/db_models.py`):** Configuração moderna de engine e sessão SQLAlchemy 2.0 com SQLite.
- **Contratos e Schemas (`backend/app/models/schemas.py`):** Modelos Pydantic v2 com validação estrita, clamp numérico automático de densidade textual ($\kappa_{\text{densidade}} \in [1.0, 2.2]$) e configuração `ConfigDict(from_attributes=True)`.
- **Conector Google Workspace (`backend/app/services/google_client.py`):**
  - Autenticação OAuth2 com renovação preventiva de `refresh_token`.
  - **Triagem Tripartida:** Isola as 10 turmas vigentes descartando turmas antigas (> 180 dias sem flag ativa prévia), turmas com bloqueio local explícito (`is_active=False`) e turmas sem publicações recentes (> 45 dias sem atividade no mural/tarefas).
  - Download binário direto para `io.BytesIO` em RAM sem gravação de arquivos temporários em disco.
- **Analisador de Documentos (`backend/app/services/document_analyzer.py`):**
  - Leitura em memória de PDFs com PyMuPDF (`fitz`), capturando número total de páginas ($N_{\text{páginas}}$) e o extrato integral das duas primeiras páginas.
  - Avaliação semântica via SDK oficial `google-genai` com Structured Outputs (`SemanticTaskEvaluation`).
  - Motor determinístico de cálculo de esforço temporal:
    $$t_{\text{base}} = 3.0 \text{ min/página}$$
    $$\lambda_{\text{idioma}} = 1.35 \text{ (se estrangeiro) senão } 1.0$$
    $$T_{\text{leitura}} = \text{round}(N_{\text{páginas}} \times t_{\text{base}} \times \kappa_{\text{densidade}} \times \lambda_{\text{idioma}})$$
    $$T_{\text{total}} = T_{\text{leitura}} + T_{\text{produção}}$$
  - Disparo de hook configurável para emissão de alertas prioritários quando `group_required == True`.

### Núcleo FastAPI, Gestão de Ciclo de Vida & Mensageria Interativa ntfy (Fase 3 — SPEC-003)

- **Núcleo do Servidor Autônomo (`backend/app/main.py`):**
  - Gerenciador de ciclo de vida assíncrono nativo `lifespan(app: FastAPI)`.
  - Inicialização automática do banco relacional via `init_db()`.
  - **APScheduler (`AsyncIOScheduler`):**
    - **Varredura Periódica a cada 2h (`IntervalTrigger(hours=2)`):** Sincroniza turmas ativas do Classroom, processa novos PDFs no Gemini (disparando alerta urgente se `group_required == True`) e agenda tarefas `PENDING` no Google Calendar.
    - **Despacho Matinal Diário às 07:30 (`CronTrigger(hour=7, minute=30)`):** Consulta alocações do dia, fila de leitura e rotinas residenciais, enviando o briefing consolidado.
    - **Rotinas Domésticas:** Carrega expressões crontab cadastradas em `domestic_routines` e agenda triggers individuais (`CronTrigger.from_crontab`).
    - Drenagem e encerramento gracioso do agendador (`wait=True`) sem perda de estado.
- **Mensageria e Protocolo ntfy (`backend/app/services/notifier.py`):**
  - Publicação assíncrona com `httpx.AsyncClient` com timeout estrito de **10 segundos**.
  - **Tolerância a oscilações de rede:** Algoritmo de retentativas com **recuo exponencial (exponential backoff)** (3 tentativas), preservando o estado local consistente.
  - **Cabeçalhos Padronizados ntfy:**
    - `Title`: Título contextual da notificação.
    - `Priority`: `5` (máxima/urgente) por padrão nos blocos de estudo, alertas de grupo e rotinas domésticas para despertar o ecrã e contornar Doze Mode no Android.
    - `Tags`: Identificadores visuais com emojis (`books,hourglass_flowing_sand`, `warning`, `house`, `sunrise`).
    - `Actions`: Contratos interativos acionáveis diretamente da notificação móvel:
      - **Concluir Tarefa:** `http, Concluir, {PUBLIC_API_BASE_URL}/api/tasks/{task_id}/complete, method=POST, clear=true`
      - **Adiar 1h (Snooze):** `http, Adiar 1h, {PUBLIC_API_BASE_URL}/api/tasks/{task_id}/snooze?min=60, method=POST, clear=true`
      - **Abrir PDF:** `view, Abrir PDF, {material_url}`
      - **Concluir Rotina Doméstica:** `http, Concluir, {PUBLIC_API_BASE_URL}/api/routines/{routine_id}/complete, method=POST, clear=true`
- **Endpoints REST e Painel da Jornada Diária (`backend/app/api/endpoints.py`):**
  - `POST /api/tasks/{task_id}/complete`: Transição transacional da tarefa para `'COMPLETED'`.
  - `POST /api/tasks/{task_id}/snooze?min=60`: Adia a tarefa a partir de `now + min`, recalculando os blocos no motor determinístico e atualizando o Google Calendar. Retorna `409 Conflict` se violar margem de 24h ou capacidade.
  - `POST /api/routines/{routine_id}/complete`: Marca conclusão de obrigação residencial (`last_completed = now_utc`).
  - `GET /api/dashboard/daily`: Retorna payload estruturado completo (`classes_and_allocations`, `reading_queue` ordenada e `domestic_routines` com flag `is_completed_today`).
  - `GET /health`: Healthcheck do microsserviço informando integridade e estado do agendador.

---

## 🗄️ 3. Esquema das Tabelas do Banco de Dados

| Tabela | Coluna | Tipo | Restrições | Descrição |
| :--- | :--- | :--- | :--- | :--- |
| **`courses`** | `id` | VARCHAR(64) | Primary Key | ID oficial da turma no Google Classroom |
| | `name` | VARCHAR(255) | NOT NULL | Nome da disciplina |
| | `is_active` | BOOLEAN | DEFAULT TRUE | Filtra turmas ativas do semestre vigente |
| | `last_synced_at` | DATETIME | NULLABLE | Timestamp UTC da última sincronização |
| **`materials`** | `id` | VARCHAR(128) | Primary Key | ID do arquivo no Google Drive ou URL |
| | `course_id` | VARCHAR(64) | ForeignKey(`courses.id`) | Turma vinculada |
| | `title` | VARCHAR(255) | NOT NULL | Nome do material ou documento |
| | `page_count` | INTEGER | DEFAULT 0 | Contagem total de páginas do PDF |
| | `lexical_density` | FLOAT | DEFAULT 1.0 | Fator $\kappa$ de densidade textual calibrado |
| **`tasks`** | `id` | VARCHAR(64) | Primary Key | ID da tarefa no Classroom (`courseWork.id`) |
| | `course_id` | VARCHAR(64) | ForeignKey(`courses.id`) | Turma vinculada |
| | `title` | VARCHAR(255) | NOT NULL | Título do trabalho ou atividade |
| | `due_datetime` | DATETIME | NOT NULL | Data e hora de entrega |
| | `est_duration_min` | INTEGER | NOT NULL | Tempo total estimado em minutos ($T_{\text{total}}$) |
| | `group_required` | BOOLEAN | DEFAULT FALSE | Flag que indica exigência de trabalho em grupo |
| | `status` | VARCHAR(32) | DEFAULT 'PENDING' | Status (`PENDING`, `SCHEDULED`, `COMPLETED`) |
| **`calendar_allocations`** | `id` | INTEGER | PK, Autoincrement | Identificador do slot alocado |
| | `task_id` | VARCHAR(64) | ForeignKey(`tasks.id`) | Tarefa associada |
| | `gcal_event_id` | VARCHAR(128) | NOT NULL | ID do evento gerado no Google Calendar |
| | `start_time` | DATETIME | NOT NULL | Início do bloco de estudo |
| | `end_time` | DATETIME | NOT NULL | Término do bloco de estudo |
| **`domestic_routines`** | `id` | INTEGER | PK, Autoincrement | Identificador da rotina doméstica |
| | `name` | VARCHAR(128) | NOT NULL | Nome da atividade fixa |
| | `cron_expression` | VARCHAR(64) | NOT NULL | Expressão cron de recorrência (ex: `0 20 * * 2,4`) |
| | `last_completed` | DATETIME | NULLABLE | Data de conclusão mais recente |

---

## ⚙️ 4. Pré-requisitos e Configuração do Ambiente

### Requisitos
- **Python:** 3.11+ (homologado em Python 3.13)
- **Virtualenv:** `/root/.venv` (ou venv local de preferência)

### Instalação de Dependências
```bash
/root/.venv/bin/pip install --upgrade pip
/root/.venv/bin/pip install -r backend/requirements.txt
```

### Variáveis de Ambiente
Crie um arquivo `.env` (ignorado pelo git) com as seguintes configurações:
```bash
# Chave de acesso à API do Gemini
GEMINI_API_KEY="AIzaSy..."

# Caminhos das credenciais do Google Workspace OAuth2
GOOGLE_CREDENTIALS_PATH="credentials.json"
GOOGLE_TOKEN_PATH="token.json"

# String de conexão do banco de dados (SQLite por padrão)
DATABASE_URL="sqlite:///academic_platform.db"

# Parâmetros do Protocolo ntfy
NTFY_SERVER_URL="https://ntfy.sh"
NTFY_TOPIC="assistente_academico_dev"
PUBLIC_API_BASE_URL="http://localhost:8000"

# Fuso Horário
TIMEZONE="America/Sao_Paulo"
```

---

## 🧪 5. Execução dos Testes com Pytest

A suíte de testes utiliza banco SQLite em memória (`sqlite:///:memory:` com `StaticPool`) e mocks completos das chamadas de rede da Google API (Classroom, Drive, Calendar), Gemini e ntfy:

```bash
# Executa todos os testes do projeto (SPEC-001, SPEC-002 e SPEC-003)
/root/.venv/bin/pytest -v tests/

# Execução individual por especificação
/root/.venv/bin/pytest -v tests/test_ingestion_and_analyzer.py
/root/.venv/bin/pytest -v tests/test_scheduler_engine.py
/root/.venv/bin/pytest -v tests/test_api_and_notifier.py
```

### Cobertura da Suíte de Testes (26 Testes de Integração)

#### Ingestão e Análise Semântica (SPEC-001):
1. `test_schema_table_creation`: Garante a criação de todas as 5 tabelas e colunas no SQLAlchemy 2.0.
2. `test_filter_active_courses_discard_logic`: Valida os 3 filtros determinísticos de descarte de turmas inativas/antigas.
3. `test_in_memory_pdf_reading_no_disk`: Assegura a extração de páginas e texto estritamente em memória com PyMuPDF.
4. `test_effort_calculation_mathematics_and_clamp`: Valida o clamp $\kappa \in [1.0, 2.2]$, penalidade de língua estrangeira ($\lambda = 1.35$) e os tempos $T_{\text{leitura}}$ e $T_{\text{total}}$.
5. `test_priority_alert_and_persistence`: Testa o disparo do hook prioritário para trabalho em grupo e a persistência relacional completa.
6. `test_no_alert_when_group_not_required`: Confirma que tarefas individuais não acionam o alerta prioritário.

#### Motor de Agendamento Linear e Sincronização Google Calendar (SPEC-002):
7. `test_cognitive_chunking_math`: Valida o fracionamento em blocos ($\le 90$ min vs $> 90$ min, ex: 140 min $\rightarrow$ 2 de 70 min; 160 min $\rightarrow$ 3 de 54 min).
8. `test_busy_intervals_merging_and_window_subtraction`: Valida a fusão de intervalos ocupados do Calendar e recorte nas janelas permitidas (08:30–11:30 e 14:00–21:00).
9. `test_mandatory_10min_transition_buffer`: Garante que blocos exigem $d_{\text{bloco}} + 20$ min contínuos, com 10 min de resguardo antes e depois.
10. `test_antivespera_24h_margin_and_insufficient_window`: Garante término $\le \text{due} - 24\text{h}$ e lança `InsufficientScheduleWindowError` se inviável.
11. `test_schedule_capacity_exceeded_error_when_saturated`: Confirma disparo de `ScheduleCapacityExceededError` sem marcações parciais se a agenda estiver lotada.
12. `test_calendar_sync_and_persistence_mocked`: Valida persistência atômica em `calendar_allocations`, status `'SCHEDULED'`, metadados do evento e idempotência contra duplicatas.

#### Núcleo FastAPI, APScheduler e Mensageria ntfy (SPEC-003):
13. `test_complete_task_endpoint`: Transição transacional para `COMPLETED` e tratamento de 404 para tarefas inexistentes.
14. `test_snooze_task_endpoint_success`: Recálculo linear do agendamento com recuo no tempo e geração de novos blocos.
15. `test_snooze_task_conflict_insufficient_window`: Disparo de `409 Conflict` quando o snooze viola a margem antivéspera de 24h.
16. `test_complete_routine_endpoint`: Atualização transacional de `last_completed` em `domestic_routines`.
17. `test_daily_dashboard_endpoint`: Validação estrita do payload completo do dashboard da jornada diária.
18. `test_daily_dashboard_invalid_date`: Tratamento de erro `400 Bad Request` para strings de data malformadas.
19. `test_notifier_study_block_alert_headers_and_actions`: Formatação correta dos cabeçalhos ntfy (`Priority: 5`, `Tags` e `Actions` interativas).
20. `test_notifier_domestic_routine_alert_contract`: Validação do contrato do botão de confirmação de rotina doméstica.
21. `test_notifier_group_urgent_alert`: Validação do alerta de prioridade máxima para trabalho em grupo.
22. `test_notifier_oauth_emergency_alert`: Formatação do despacho de emergência por revogação/expiração de token.
23. `test_notifier_retry_and_backoff_transient_failure`: Tolerância a falhas com 2 falhas de rede simuladas e sucesso na 3ª tentativa.
24. `test_notifier_exhausted_retries_resilience`: Resiliência contra quedas permanentes sem quebra de fluxo na aplicação.
25. `test_healthcheck_endpoint`: Verificação de integridade e telemetria dos jobs do agendador em `GET /health`.
26. `test_lifespan_initialization_and_shutdown`: Inicialização do SQLite, registro de jobs no `AsyncIOScheduler` e drenagem limpa no encerramento.

