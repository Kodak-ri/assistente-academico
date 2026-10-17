# 📖 Guia Passo a Passo: Como Instalar e Usar o Assistente Acadêmico (Para Qualquer Pessoa)

Bem-vindo(a)! Este guia foi escrito **sem termos técnicos complicados** para que qualquer estudante consiga colocar o **Assistente Acadêmico (`AcadAgent`)** funcionando do zero, no computador ou servidor, e instalá-lo no celular.

---

## 💡 O que este assistente faz por você na prática?

Imagine um secretário pessoal de estudos que trabalha 24 horas por dia:
1. **Olha seu Google Classroom sozinho:** A cada 2 horas, ele entra nas suas matérias ativas da faculdade e vê se o professor postou algum PDF ou trabalho novo.
2. **Lê o PDF e calcula o tempo de estudo:** Ele conta quantas páginas o PDF tem e usa Inteligência Artificial (Google Gemini) para descobrir se o texto é fácil, difícil, em outro idioma ou se é **trabalho em grupo**.
3. **Agenda na sua Google Agenda automaticamente:** Em vez de deixar você acumular tudo para a véspera, ele divide leituras grandes em blocos de até **1h15min** nos seus horários livres (manhã ou tarde/noite), sempre terminando **1 dia antes da entrega**.
4. **Avisa no seu celular e tem um Aplicativo próprio:** Todo dia às **07:30 da manhã** ele manda o resumo do dia no seu celular, avisa na hora de estudar e ainda lembra das tarefas de casa (como trocar roupa de cama ou colocar o lixo para fora).

---

## 🧰 O que você vai precisar antes de começar? (10 minutos)

Você só precisa de **3 coisas gratuitas**:
1. Uma **Chave do Google Gemini** (gratuita, leva 1 minuto).
2. Um **Arquivo de Login do Google** (`credentials.json`) para o assistente ler seu Classroom e sua Agenda.
3. O aplicativo gratuito **ntfy** instalado no seu celular para receber os avisos com botões.

Vamos fazer cada um deles passo a passo abaixo!

---

## Passo 1: Pegar sua Chave Gratuita da IA (Google Gemini)

1. Acesse o site oficial **[Google AI Studio](https://aistudio.google.com/app/apikey)** e faça login com sua conta Google.
2. Clique no botão azul **"Create API key"** (Criar chave de API).
3. Copie o código gerado (ele começa com `AIzaSy...`) e guarde no seu bloco de notas.

---

## Passo 2: Autorizar o acesso ao seu Classroom e Google Agenda

Para que o assistente veja suas turmas e marque horários na sua agenda, você cria uma chave pessoal no Google:

1. Acesse o **[Google Cloud Console](https://console.cloud.google.com/)**.
2. No topo da página, clique em **"Selecionar um projeto"** e depois em **"Novo Projeto"**. Dê o nome de `Meu Assistente Academico` e clique em **Criar**.
3. No menu lateral esquerdo, vá em **"APIs e Serviços" -> "Biblioteca"** e ative estas 3 opções (basta pesquisar o nome, clicar nele e clicar no botão azul **Ativar**):
   - **Google Classroom API**
   - **Google Drive API**
   - **Google Calendar API**
4. No menu esquerdo, clique em **"Tela de permissão OAuth"**:
   - Escolha **Externo** e clique em **Criar**.
   - Preencha apenas o nome (`Assistente Academico`) e seu e-mail nos campos obrigatórios.
   - Na etapa **"Usuários de teste"**, clique em **"+ Add Users"** e digite o **seu próprio e-mail do Google Classroom**. Salve tudo.
5. Agora vá em **"Credenciais"** (no menu esquerdo):
   - Clique em **"+ Criar Credenciais"** no topo -> **"ID do cliente OAuth"**.
   - Em *Tipo de aplicativo*, escolha **"App para computador"** (Desktop App) e clique em **Criar**.
   - Clique em **"Fazer download do JSON"**.
   - Renomeie o arquivo baixado para exatamente **`credentials.json`** e coloque-o dentro da pasta do projeto.

---

## Passo 3: Configurar as Notificações no Celular (App ntfy)

O **ntfy** é um aplicativo gratuito que recebe alertas instantâneos com botões de ação (**"Concluir"**, **"Adiar 1h"** e **"Abrir PDF"**):

1. Baixe o aplicativo **ntfy** na **[Google Play Store (Android)](https://play.google.com/store/apps/details?id=io.heckel.ntfy)** ou na **[App Store (iPhone)](https://apps.apple.com/us/app/ntfy/id1625396347)**.
2. Abra o aplicativo, toque no botão **`+`** (Inscrever-se em um tópico).
3. Invente um nome único e secreto para o seu canal (por exemplo: `estudos_joao_98765`).
   > 💡 **Dica:** Escolha um nome com números ou palavras que só você conheça. Esse será o seu `NTFY_TOPIC`.

---

## Passo 4: Baixar e Ligar o Assistente

Comece baixando o projeto no seu computador ou servidor:

```bash
git clone https://github.com/Kodak-ri/assistente-academico.git
cd assistente-academico
```

### 4.1. Preencha suas configurações (`.env`)
Copie o arquivo de modelo para criar a sua configuração pessoal:

```bash
cp .env.example .env
```

Abra o arquivo `.env` no Bloco de Notas (ou com `nano .env`) e preencha apenas os 3 campos principais:

```env
# 1. Cole aqui a chave que você pegou no Passo 1:
GEMINI_API_KEY="AIzaSySuaChaveAqui..."

# 2. Coloque aqui o nome do canal que você criou no app ntfy no Passo 3:
NTFY_TOPIC="estudos_joao_98765"

# 3. Endereço do seu computador/servidor na sua rede Wi-Fi (para os botões do celular funcionarem):
PUBLIC_API_BASE_URL="http://localhost:8000"
```

### 4.2. Faça o primeiro login na sua conta Google (Só precisa fazer 1 vez!)
Antes de deixar o sistema rodando sozinho, autorize o acesso à sua conta Google executando:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -c "from backend.app.services.google_client import GoogleWorkspaceClient; GoogleWorkspaceClient().authenticate(); print('✅ Login Google concluído com sucesso!')"
```
> O navegador vai abrir pedindo para você escolher sua conta Google e clicar em **Permitir**. Isso criará o arquivo `token.json` automaticamente e você não precisará mais fazer login!

### 4.3. Escolha como ligar o Assistente

#### Opção A: Usando Docker (Recomendado — roda sozinho em segundo plano)
Se você tem o Docker instalado, basta 1 comando:

```bash
docker compose up -d --build
```
Pronto! Ele já está rodando e reiniciará sozinho mesmo se o computador desligar.

#### Opção B: Usando Python diretamente no Terminal
Se não quiser usar Docker, rode:

```bash
.venv/bin/uvicorn backend.app.main:app --host 0.0.0.0 --port 8000
```

---

## 📱 Passo 5: Como Usar no Dia a Dia e Instalar o App no Celular

Com o assistente ligado, abra o navegador do seu computador ou celular e acesse:
👉 **`http://localhost:8000`** *(ou `http://IP_DO_SEU_COMPUTADOR:8000` pelo celular no mesmo Wi-Fi)*.

### Como instalar na tela inicial do celular (Igual a um App nativo):
- **No Android (Chrome):** Toque nos **3 pontinhos** no canto superior direito e selecione **"Instalar aplicativo"** (ou *"Adicionar à tela inicial"*).
- **No iPhone (Safari):** Toque no botão **Compartilhar** (quadradinho com uma seta para cima na barra inferior) e escolha **"Adicionar à Tela de Início"**.

---

## 🖥️ Entendendo a Tela do Aplicativo (3 Seções Simples)

O painel foi desenhado para você bater o olho e saber exatamente o que fazer hoje:

### 1. Topo da Tela (Resumo do Dia)
Mostra quantos **minutos de estudo** você tem hoje, quantas **páginas faltam ler** e se você está conectado (**Online** ou **Modo Offline**).

### 2. Seção "Aulas e Blocos de Estudo"
Mostra em ordem de horário todos os seus blocos de estudo agendados para hoje (ex.: `10:10 – 11:16 | Sistemas Distribuídos`).

### 3. Seção "Fila Ordenada de Leitura"
Lista todos os seus trabalhos e PDFs pendentes organizados do prazo mais urgente para o mais distante:
- Mostra um selo com a **quantidade de páginas** (ex: `30 págs`) e o **tempo estimado** (ex: `197 min`).
- Se o trabalho for em dupla ou grupo, aparece um aviso vermelho piscante **"Trabalho em Grupo"**.
- Possui 3 botões grandes e fáceis de tocar:
  - **✅ Concluir:** Marca a tarefa como pronta na hora (ela sai da fila imediatamente).
  - **⏰ Adiar 1h:** Está ocupado agora? Toque aqui e o assistente empurra seu bloco de estudo para mais tarde automaticamente na sua Google Agenda!
  - **📄 Abrir PDF:** Abre o arquivo PDF direto no seu Google Drive para você começar a ler.

### 4. Seção "Manutenção Doméstica"
Lista suas tarefas de casa (como *"Trocar Roupa de Cama"* ou *"Colocar o Lixo para Fora"*). Quando você fizer a tarefa, basta tocar no botão **Concluir** e ela fica marcada com um selo verde **"Concluída Hoje"**.

---

## 🏠 Bônus: Como Cadastrar Suas Próprias Tarefas de Casa?

Quer que o assistente lembre você de regar as plantas, limpar a caixa de areia do gato ou trocar a roupa de cama em dias específicos?
Basta rodar este comando rápido no terminal (altere o nome e os dias como preferir):

```bash
.venv/bin/python -c "
from backend.app.core.database import SessionLocal, init_db
from backend.app.models.db_models import DomesticRoutine

init_db()
with SessionLocal() as db:
    # Exemplo: Toda terça (2) e quinta (4) às 20:00 ('0 20 * * 2,4')
    db.add(DomesticRoutine(name='Colocar o Lixo para Fora', cron_expression='0 20 * * 2,4'))
    # Exemplo: Todo sábado (6) às 10:00 ('0 10 * * 6')
    db.add(DomesticRoutine(name='Trocar Roupa de Cama', cron_expression='0 10 * * 6'))
    db.commit()
print('✅ Rotinas cadastradas com sucesso!')
"
```

Reinicie o assistente e ele já agendará os avisos automáticos para esses horários!

---

## ❓ Perguntas Frequentes (Se algo der errado)

**1. "Cliquei em *Adiar 1h* e apareceu um aviso amarelo de Prazo Apertado (`409`). O que significa?"**
Significa que a entrega do trabalho já está a menos de 24 horas ou sua agenda está 100% cheia até a véspera. Para proteger você de perder o prazo, o assistente avisa que não é seguro adiar mais e mantém o horário atual.

**2. "Posso abrir o aplicativo no celular mesmo sem internet?"**
Sim! O aplicativo guarda automaticamente na memória do celular a última programação carregada. Mesmo no metrô ou sem sinal, você consegue abrir o app e consultar suas leituras e horários do dia.

**3. "Como sei se o sistema está saudável e funcionando?"**
Acesse `http://localhost:8000/health` no navegador. Ele mostrará `"status": "healthy"` e a lista de robôs internos ativos (`periodic_scan` e `daily_briefing`).
