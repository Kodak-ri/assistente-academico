# RFC-001: Integração de Interfaces Google Cloud MCP e Arquitetura Agêntica Híbrida

* **Status:** Proposta de Ideia / RFC em Discussão
* **Autor:** Agente OMV (Agy) & Usuário
* **Data:** 18 de Outubro de 2026
* **Projeto:** Assistente Acadêmico Autônomo (`AcadAgent`)
* **Branch:** `idea/google-cloud-mcp-architecture`

---

## 1. Resumo Executivo e Motivação

Durante a configuração das credenciais OAuth 2.0 no Google Cloud Console e a análise das APIs do Google Workspace, foi identificada a nova opção oficial:
> *"Use this client for an AI-powered agent — Designates this client for AI agents that take actions on behalf of users. Select this if your client uses Google MCP tools. If your application supports both AI agent workflows and standard user features, use separate OAuth clients for each."*

Adicionalmente, a Google disponibilizou interfaces e servidores **MCP (Model Context Protocol)** para ecossistemas de IA, incluindo o **Classroom MCP (`classroommcp.googleapis.com`)** e servidores de ferramentas de Workspace (Drive, Calendar, etc.).

Esta RFC avalia rigorosamente:
1. **O que as interfaces MCP facilitariam** no fluxo de trabalho do Assistente Acadêmico.
2. **Onde o MCP NÃO deve ser utilizado** (para evitar armadilhas de custo, lentidão e perda de precisão matemática).
3. **A proposta de Arquitetura Agêntica Híbrida ("Dual-Engine")**: a combinação ideal entre o motor determinístico atual e uma camada interativa orientada a MCP.

---

## 2. Diagnóstico Técnico: Arquitetura Atual vs. Paradigma MCP Puro

Atualmente, o Assistente Acadêmico opera com um **motor batch e determinístico**:
* **Varredura e Ingestão:** Consulta direta às APIs REST do Google Classroom, Drive e Calendar via biblioteca oficial Python (`google-api-python-client`), consumindo 0 tokens de LLM.
* **Processamento de PDFs:** Download de fluxos binários (bytes) do Google Drive em memória RAM, contagem de páginas e extração de texto via `PyMuPDF` (FitZ).
* **Análise Semântica:** Chamada pontual à API Gemini (`google-genai`) para estimativa de esforço, densidade textual e tema.
* **Agendamento:** Algoritmo matemático estrito (`scheduler_engine.py`) com buffers de 10 min, limite de janela cognitiva de 75 min e trava anti-véspera de 24h.
* **Persistência e Interface:** Banco de dados SQLite local, notificações interativas via `ntfy.sh` e PWA responsivo em Tailwind/Vanilla JS.

### Matriz Comparativa de Viabilidade

| Vetor de Avaliação | Fluxo Atual (REST APIs Diretas) | Abordagem 100% MCP (Google MCP Tools) | Veredito Técnico |
| :--- | :--- | :--- | :--- |
| **Execução Periódica em Segundo Plano** | **Excelente (Ideal)**: Roda a cada 2h no background, silencioso, sem custo de tokens para varrer turmas. | **Ineficiente**: Exige invocação contínua de turnos de LLM para orquestrar chamadas JSON-RPC. | Manter REST APIs no core de background |
| **Download e Extração de PDFs Pesados** | **Excelente**: Streaming direto de binários para RAM, processado em milissegundos pelo PyMuPDF. | **Inadequado**: Protocolo MCP baseado em JSON-RPC não é projetado para tráfego de grandes blobs binários. | Manter Drive API direta para leitura de PDFs |
| **Agendamento de Calendário** | **Determinístico e Confiável**: Algoritmo CSP exato, zero alucinações de datas/fusos horários. | **Probabilístico**: LLMs sofrem com fusos horários, cálculo de sobreposições e restrições complexas de minutos. | Manter motor matemático em Python |
| **Custo de Operação e Cotas** | **Custo Zero** na ingestão; cota do Gemini gasta apenas na análise do PDF. | **Custo Alto**: Dezenas de chamadas de ferramenta de IA consumindo tokens e limites por minuto do Gemini. | REST vence com folga em eficiência de custos |
| **Interação em Linguagem Natural** | **Nenhuma**: O PWA só tem botões estáticos ("Concluir", "Adiar"). | **Excepcional**: Permite ao aluno dialogar, solicitar mudanças de rotina e tirar dúvidas sobre os textos. | **Oportunidade de Ouro para o MCP!** |
| **Interoperabilidade com outros Agentes** | **Fechada**: O assistente só fala consigo mesmo e com o ntfy. | **Aberta e Universal**: Qualquer agente (Agy, Claude, Cursor, Maestro) pode usar as ferramentas do assistente. | **Oportunidade de Ouro para o MCP!** |

---

## 3. Onde o MCP Realmente Facilita e Agrega Valor Revolucionário

A adoção de interfaces MCP não deve substituir a fundação estável do assistente, mas sim desbloquear **dois novos pilares transformadores**:

### Pilar A: O Assistente Acadêmico como Servidor MCP (`AcadAgent MCP Server`)
Em vez de apenas o assistente consumir MCPs externos, o próprio `assistente-academico` pode expor um **servidor MCP** com ferramentas especializadas conectadas ao seu banco SQLite e ao Google Calendar:

1. **`get_today_schedule()`**: Retorna os blocos de estudo e compromissos do dia para qualquer IA.
2. **`get_reading_backlog(course_id=None)`**: Lista textos pendentes com tempo estimado e links do Drive.
3. **`reschedule_study_block(block_id, reason, preferred_time)`**: Aciona o motor determinístico em segundo plano a partir de um pedido informal do usuário.
4. **`ask_about_reading_material(task_id, question)`**: Utiliza o resumo e o texto do PDF já indexados no banco para responder dúvidas acadêmicas do estudante.

*Benefício Prático:* O estudante poderá abrir o chat do Antigravity CLI, Claude Desktop, Cursor ou OpenWebUI e dizer:
> *"Agy, hoje tive um imprevisto e só posso estudar depois das 19h. Reorganize meus blocos de Teoria Política no calendário."*  
> O agente chamará a ferramenta MCP do assistente, que executará a matemática perfeita sem o usuário precisar entrar na agenda manualmente.

### Pilar B: Chat Conversacional Integrado ao PWA com Tool-Calling
Com a introdução de uma aba de chat no PWA:
* O estudante pode interagir diretamente com o Gemini 3.8 Flash dentro do aplicativo instalado no celular.
* O Gemini é equipado com as ferramentas de consulta ao banco e às APIs do Workspace (Google MCP tools).
* Permite comandos do tipo:
  * *"Quais avisos importantes foram postados no Classroom de Relações Internacionais esta semana?"*
  * *"Faça um roteiro de 5 tópicos principais para a leitura de amanhã de Métodos Quantitativos."*

### Pilar C: Formalização de Credencial OAuth "AI-Powered Agent"
A opção no Google Cloud Console de marcar a credencial como **"AI-powered agent client"** é altamente recomendada se decidirmos implementar a camada conversacional:
* Garante conformidade com os novos termos de segurança do Google Workspace for AI.
* Concede cotas e tratamento adequados para fluxos de *delegated agent actions* sem disparar alarmes falsos de aplicativos não verificados ou abusivos.

---

## 4. Arquitetura Proposta: O Modelo Híbrido "Dual-Engine"

```
+---------------------------------------------------------------------------------+
|                       ASSISTENTE ACADÊMICO (DUAL-ENGINE)                       |
+---------------------------------------------------------------------------------+
                                       │
         ┌─────────────────────────────┴─────────────────────────────┐
         ▼                                                           ▼
┌─────────────────────────────────┐                         ┌─────────────────────────────────┐
│     ENGINE 1: DETERMINÍSTICO    │                         │       ENGINE 2: AGÊNTICO        │
│    (Core Headless em Background)│                         │     (Interfaces MCP & Chat)     │
├─────────────────────────────────┤                         ├─────────────────────────────────┤
│ • Varredura a cada 2h (Cron)    │                         │ • Servidor MCP FastAPI/SSE      │
│ • Google REST APIs Diretas      │                         │ • Chat Conversacional no PWA    │
│ • Streaming de PDFs em RAM      │                         │ • Ferramentas Google Workspace  │
│ • PyMuPDF + Gemini Analyzer     │                         │ • Resumos e Q&A Semântico       │
│ • Algoritmo Matemático (CSP)    │                         │ • Comandos em Linguagem Natural │
│ • Push Notifications (ntfy.sh)  │                         │ • Conexão com Antigravity/Agy   │
└────────────────┬────────────────┘                         └────────────────┬────────────────┘
                 │                                                           │
                 └─────────────────────────────┬─────────────────────────────┘
                                               ▼
                                  ┌────────────────────────┐
                                  │   SQLite Local + Cache │
                                  │(Tarefas, Blocos, Cursos│
                                  └────────────────────────┘
```

---

## 5. Plano de Implementação Sugerido (Fases)

### Fase 1: Padronização e OAuth
* Atualizar documentação e registrar cliente OAuth dedicado para agentes com a tag "AI-powered agent client" caso a funcionalidade conversacional seja aprovada.

### Fase 2: Exposição de Ferramentas MCP no Backend
* Implementar módulo `backend/app/mcp/` utilizando a biblioteca `mcp` (Model Context Protocol SDK para Python).
* Expor endpoints SSE (`/mcp/sse`) ou suporte stdio para permitir que assistentes externos (Agy, OpenWebUI) controlem a rotina acadêmica.

### Fase 3: Camada de Chat no PWA
* Adicionar gaveta/aba de chat no PWA mobile-first existente.
* Integrar com `google-genai` usando tool-calling vinculado às rotinas locais do banco de dados.

---

## 6. Conclusão e Recomendação

**Veredito:** As interfaces MCP da Google Cloud e o ecossistema MCP **facilitam significativamente a experiência do usuário e a inteligência conversacional**, mas **NÃO devem substituir a base determinística de ingestão e agendamento** do assistente.

A adoção do **Modelo Híbrido Dual-Engine** extrai o melhor dos dois mundos:
1. **Robustez, velocidade e custo zero** no trabalho pesado de fundo.
2. **Superpoderes conversacionais e interoperabilidade universal** na ponta para o estudante.
