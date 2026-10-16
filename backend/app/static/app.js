/**
 * app.js — Motor Reativo e Mutações Otimistas (SPEC-004)
 * PWA AcadAgent: Interface Mobile-First, Vanilla ES6+, Fetch API e Gestão de Estado
 */

// Estado global em memória da aplicação
const AppState = {
  date: null,
  classes_and_allocations: [],
  reading_queue: [],
  domestic_routines: [],
  isLoading: false,
  metrics: {
    totalStudyMin: 0,
    totalPendingPages: 0,
    completedTasksCount: 0
  }
};

// =============================================================================
// 1. INICIALIZAÇÃO & SERVICE WORKER
// =============================================================================

document.addEventListener('DOMContentLoaded', () => {
  initServiceWorker();
  initNetworkListeners();
  initEventListeners();
  fetchDailyDashboard();
});

function initServiceWorker() {
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker
      .register('/service-worker.js', { scope: '/' })
      .then((reg) => {
        console.log('[PWA] Service Worker registrado com escopo:', reg.scope);
      })
      .catch((err) => {
        console.warn('[PWA] Falha ao registrar Service Worker:', err);
      });
  }
}

function initNetworkListeners() {
  const statusEl = document.getElementById('connection-status');
  if (!statusEl) return;

  function updateStatus() {
    if (navigator.onLine) {
      statusEl.className = 'inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-emerald-950 text-emerald-400 border border-emerald-800/80';
      statusEl.innerHTML = '<span class="w-1.5 h-1.5 mr-1.5 rounded-full bg-emerald-400 animate-pulse"></span>Online';
    } else {
      statusEl.className = 'inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-amber-950 text-amber-400 border border-amber-800/80';
      statusEl.innerHTML = '<span class="w-1.5 h-1.5 mr-1.5 rounded-full bg-amber-400"></span>Offline (Cache)';
    }
  }

  window.addEventListener('online', () => {
    updateStatus();
    showToast('Conexão restabelecida. Sincronizando dados...', 'info');
    fetchDailyDashboard();
  });

  window.addEventListener('offline', () => {
    updateStatus();
    showToast('Modo offline ativado. Exibindo dados locais do cache.', 'warning');
  });

  updateStatus();
}

function initEventListeners() {
  const btnRefresh = document.getElementById('btn-refresh');
  if (btnRefresh) {
    btnRefresh.addEventListener('click', () => {
      const icon = document.getElementById('refresh-icon');
      if (icon) icon.classList.add('animate-spin');
      fetchDailyDashboard().finally(() => {
        if (icon) icon.classList.remove('animate-spin');
      });
    });
  }

  // Delegação de eventos para a Fila de Leitura (Concluir, Adiar)
  const readingList = document.getElementById('reading-list');
  if (readingList) {
    readingList.addEventListener('click', (e) => {
      const target = e.target.closest('button');
      if (!target) return;

      const taskId = target.getAttribute('data-task-id');
      if (!taskId) return;

      const card = target.closest('[data-task-card]');

      if (target.classList.contains('btn-complete')) {
        handleOptimisticTaskComplete(taskId, card);
      } else if (target.classList.contains('btn-snooze')) {
        handleOptimisticTaskSnooze(taskId, target, card);
      }
    });
  }

  // Delegação de eventos para Manutenção Doméstica (Concluir)
  const routinesList = document.getElementById('routines-list');
  if (routinesList) {
    routinesList.addEventListener('click', (e) => {
      const target = e.target.closest('button');
      if (!target) return;

      const routineId = target.getAttribute('data-routine-id');
      if (!routineId) return;

      const card = target.closest('[data-routine-card]');
      if (target.classList.contains('btn-routine-complete')) {
        handleOptimisticRoutineComplete(routineId, card, target);
      }
    });
  }
}

// =============================================================================
// 2. CONSUMO ASSÍNCRONO DA API (GET /api/dashboard/daily)
// =============================================================================

async function fetchDailyDashboard(dateStr = null) {
  AppState.isLoading = true;
  const url = dateStr ? `/api/dashboard/daily?date=${encodeURIComponent(dateStr)}` : '/api/dashboard/daily';

  try {
    const response = await fetch(url);
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}: ${response.statusText}`);
    }

    const data = await response.json();
    AppState.date = data.date;
    AppState.classes_and_allocations = data.classes_and_allocations || [];
    AppState.reading_queue = data.reading_queue || [];
    AppState.domestic_routines = data.domestic_routines || [];

    recalculateMetrics();
    renderAll();
  } catch (error) {
    console.error('[Dashboard] Erro ao carregar dados:', error);
    showToast('Não foi possível atualizar o painel. Verifique a rede.', 'error');
  } finally {
    AppState.isLoading = false;
  }
}

function recalculateMetrics() {
  let pendingMinutes = 0;
  let pendingPages = 0;
  let completedCount = 0;

  for (const item of AppState.reading_queue) {
    if (item.status === 'PENDING' || item.status === 'SCHEDULED') {
      pendingMinutes += item.est_duration_min || 0;
      pendingPages += item.page_count || 0;
    } else if (item.status === 'COMPLETED') {
      completedCount++;
    }
  }

  for (const r of AppState.domestic_routines) {
    if (r.is_completed_today) {
      completedCount++;
    }
  }

  AppState.metrics.totalStudyMin = pendingMinutes;
  AppState.metrics.totalPendingPages = pendingPages;
  AppState.metrics.completedTasksCount = completedCount;
}

// =============================================================================
// 3. RENDERIZAÇÃO VISUAL DA INTERFACE
// =============================================================================

function renderAll() {
  renderHeaderStatus();
  renderDailySchedule();
  renderReadingQueue();
  renderDomesticRoutines();
}

function renderHeaderStatus() {
  const dateEl = document.getElementById('current-date');
  const studyEl = document.getElementById('total-study-time');
  const pagesEl = document.getElementById('total-pending-pages');
  const tasksEl = document.getElementById('total-completed-tasks');

  if (dateEl && AppState.date) {
    try {
      const [year, month, day] = AppState.date.split('-').map(Number);
      const d = new Date(year, month - 1, day);
      const formatted = d.toLocaleDateString('pt-BR', {
        weekday: 'long',
        day: 'numeric',
        month: 'long',
        year: 'numeric'
      });
      dateEl.textContent = formatted.charAt(0).toUpperCase() + formatted.slice(1);
    } catch {
      dateEl.textContent = AppState.date;
    }
  }

  if (studyEl) studyEl.textContent = `${AppState.metrics.totalStudyMin} min`;
  if (pagesEl) pagesEl.textContent = `${AppState.metrics.totalPendingPages} pág`;
  if (tasksEl) tasksEl.textContent = `${AppState.metrics.completedTasksCount} feitas`;
}

// Secção 1: Grelha de Aulas e Blocos de Estudo
function renderDailySchedule() {
  const container = document.getElementById('allocations-list');
  const countBadge = document.getElementById('allocations-count-badge');
  if (!container) return;

  const items = AppState.classes_and_allocations;
  if (countBadge) countBadge.textContent = `${items.length} blocos`;

  if (!items || items.length === 0) {
    container.innerHTML = `
      <div class="p-6 text-center text-slate-400 bg-slate-850/60 rounded-2xl border border-slate-800">
        <p class="text-sm font-medium text-slate-300">Nenhum bloco de estudo agendado para hoje.</p>
        <p class="text-xs text-slate-500 mt-1">Os blocos confirmados do Google Calendar aparecerão aqui.</p>
      </div>
    `;
    return;
  }

  container.innerHTML = items.map((alloc) => {
    const startTimeStr = formatTime(alloc.start_time);
    const endTimeStr = formatTime(alloc.end_time);
    const isCompleted = alloc.status === 'COMPLETED';

    return `
      <div class="transition-card p-3.5 bg-slate-850 hover:bg-slate-800/80 rounded-xl border ${isCompleted ? 'border-emerald-800/40 bg-emerald-950/20' : 'border-slate-800'} flex items-center justify-between gap-3 shadow-sm">
        <div class="flex items-start gap-3 min-w-0">
          <div class="flex flex-col items-center justify-center px-2.5 py-1.5 rounded-lg bg-slate-800 border border-slate-700/80 text-center shrink-0">
            <span class="text-xs font-bold text-blue-400 font-mono">${startTimeStr}</span>
            <span class="text-[10px] text-slate-400 font-mono">${endTimeStr}</span>
          </div>
          <div class="min-w-0">
            <h3 class="text-sm font-semibold text-slate-100 truncate ${isCompleted ? 'line-through text-slate-400' : ''}">${escapeHtml(alloc.task_title)}</h3>
            <p class="text-xs text-slate-400 truncate mt-0.5">${escapeHtml(alloc.course_name)}</p>
          </div>
        </div>
        <div class="shrink-0 flex items-center gap-1.5">
          <span class="inline-flex items-center px-2 py-0.5 rounded-md text-[11px] font-medium ${isCompleted ? 'bg-emerald-950 text-emerald-400 border border-emerald-800' : 'bg-blue-950 text-blue-300 border border-blue-800/60'}">
            ${isCompleted ? 'Concluído' : 'Alocado'}
          </span>
        </div>
      </div>
    `;
  }).join('');
}

// Secção 2: Fila Ordenada de Leitura
function renderReadingQueue() {
  const container = document.getElementById('reading-list');
  const countBadge = document.getElementById('reading-count-badge');
  if (!container) return;

  const items = AppState.reading_queue;
  if (countBadge) countBadge.textContent = `${items.length} itens`;

  if (!items || items.length === 0) {
    container.innerHTML = `
      <div class="p-6 text-center text-slate-400 bg-slate-850/60 rounded-2xl border border-slate-800">
        <p class="text-sm font-medium text-slate-300">🎉 Fila de leitura zerada!</p>
        <p class="text-xs text-slate-500 mt-1">Nenhum texto acadêmico pendente no momento.</p>
      </div>
    `;
    return;
  }

  container.innerHTML = items.map((task) => {
    const isCompleted = task.status === 'COMPLETED';
    const dueFormatted = formatDueDateTime(task.due_datetime);

    return `
      <div data-task-card data-task-id="${escapeHtml(task.task_id)}" class="transition-card p-4 bg-slate-850 rounded-2xl border ${isCompleted ? 'border-emerald-800/50 bg-emerald-950/20' : 'border-slate-800'} shadow-md space-y-3">
        <!-- Cabeçalho do Card: Título e Disciplina -->
        <div class="flex items-start justify-between gap-2">
          <div class="min-w-0">
            <span class="inline-block text-[11px] font-semibold text-blue-400 uppercase tracking-wider mb-1">${escapeHtml(task.course_name)}</span>
            <h3 class="text-base font-bold text-slate-100 leading-snug ${isCompleted ? 'line-through text-slate-400' : ''}">${escapeHtml(task.title)}</h3>
          </div>
          <span class="shrink-0 text-xs px-2 py-0.5 rounded-full font-medium ${isCompleted ? 'bg-emerald-900/60 text-emerald-300 border border-emerald-700' : 'bg-slate-800 text-slate-300 border border-slate-700'}">
            ${isCompleted ? '✓ Concluído' : 'Pendente'}
          </span>
        </div>

        <!-- Badges de Métricas e Prazo -->
        <div class="flex flex-wrap items-center gap-2 text-xs">
          <span class="inline-flex items-center px-2.5 py-1 rounded-lg bg-slate-800 border border-slate-700/80 text-amber-300 font-medium">
            📖 ${task.page_count} pág
          </span>
          <span class="inline-flex items-center px-2.5 py-1 rounded-lg bg-slate-800 border border-slate-700/80 text-blue-300 font-medium">
            ⏱️ ${task.est_duration_min} min
          </span>
          <span class="inline-flex items-center px-2.5 py-1 rounded-lg bg-slate-800 border border-slate-700/80 text-slate-300 font-medium">
            📅 ${dueFormatted}
          </span>
        </div>

        <!-- Alerta de Trabalho em Grupo (Destaque Urgente) -->
        ${task.group_required ? `
          <div class="p-2.5 rounded-xl bg-amber-950/60 border border-amber-600/80 text-amber-200 text-xs font-semibold flex items-center gap-2">
            <span class="text-base">⚠️</span>
            <span>Trabalho em Equipe / Dupla Obrigatório (Enunciado ou material requer divisão em grupo)</span>
          </div>
        ` : ''}

        <!-- Ações Táteis Mobile -->
        <div class="pt-2 border-t border-slate-800 flex items-center gap-2">
          ${!isCompleted ? `
            <button class="btn-complete touch-target flex-1 px-3 py-2 rounded-xl bg-emerald-600 hover:bg-emerald-500 active:scale-95 text-white font-semibold text-xs transition shadow-sm flex items-center justify-center gap-1.5" data-task-id="${escapeHtml(task.task_id)}">
              <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M5 13l4 4L19 7"/>
              </svg>
              Concluir
            </button>
            <button class="btn-snooze touch-target px-3.5 py-2 rounded-xl bg-slate-800 hover:bg-slate-700 active:scale-95 text-slate-300 hover:text-white font-semibold text-xs transition border border-slate-700 flex items-center justify-center gap-1.5" data-task-id="${escapeHtml(task.task_id)}">
              <svg class="w-4 h-4 text-amber-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z"/>
              </svg>
              Adiar 1h
            </button>
          ` : `
            <span class="text-xs text-emerald-400 font-medium flex items-center gap-1 py-1.5">
              ✓ Tarefa finalizada com sucesso
            </span>
          `}

          ${task.drive_url ? `
            <a href="${escapeHtml(task.drive_url)}" target="_blank" rel="noopener noreferrer" class="touch-target px-3 py-2 rounded-xl bg-slate-800/80 hover:bg-slate-700 active:scale-95 text-blue-400 hover:text-blue-300 text-xs font-semibold border border-slate-700 flex items-center justify-center gap-1.5 shrink-0" title="Abrir PDF no Google Drive">
              <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14"/>
              </svg>
              PDF
            </a>
          ` : ''}
        </div>
      </div>
    `;
  }).join('');
}

// Secção 3: Manutenção Doméstica
function renderDomesticRoutines() {
  const container = document.getElementById('routines-list');
  const countBadge = document.getElementById('routines-count-badge');
  if (!container) return;

  const items = AppState.domestic_routines;
  if (countBadge) countBadge.textContent = `${items.length} rotinas`;

  if (!items || items.length === 0) {
    container.innerHTML = `
      <div class="p-6 text-center text-slate-400 bg-slate-850/60 rounded-2xl border border-slate-800 col-span-full">
        <p class="text-sm font-medium text-slate-300">Nenhuma rotina doméstica cadastrada.</p>
      </div>
    `;
    return;
  }

  container.innerHTML = items.map((routine) => {
    const isCompleted = routine.is_completed_today;
    const lastCompletedFormatted = routine.last_completed ? formatTime(routine.last_completed) : 'Nunca';

    return `
      <div data-routine-card data-routine-id="${routine.id}" class="transition-card p-3.5 bg-slate-850 rounded-xl border ${isCompleted ? 'border-emerald-800/40 bg-emerald-950/20' : 'border-slate-800'} flex flex-col justify-between gap-3 shadow-sm">
        <div class="flex items-start justify-between gap-2">
          <div>
            <h3 class="text-sm font-bold text-slate-100 ${isCompleted ? 'line-through text-slate-400' : ''}">${escapeHtml(routine.name)}</h3>
            <p class="text-[11px] text-slate-400 mt-0.5">Recorrência: <code class="px-1 py-0.5 rounded bg-slate-800 text-slate-300 font-mono text-[10px]">${escapeHtml(routine.cron_expression)}</code></p>
          </div>
          <span class="routine-status-badge text-[10px] px-2 py-0.5 rounded-md font-semibold ${isCompleted ? 'bg-emerald-950 text-emerald-400 border border-emerald-800' : 'bg-slate-800 text-slate-400 border border-slate-700'}">
            ${isCompleted ? 'Feito hoje' : 'Pendente'}
          </span>
        </div>

        <div class="pt-2 border-t border-slate-800/80 flex items-center justify-between gap-2">
          <span class="text-[11px] text-slate-500 last-completed-text">Última: ${lastCompletedFormatted}</span>
          <button class="btn-routine-complete touch-target px-3 py-1.5 rounded-lg ${isCompleted ? 'bg-slate-800 text-slate-500 cursor-not-allowed' : 'bg-emerald-600 hover:bg-emerald-500 active:scale-95 text-white shadow-sm'} font-semibold text-xs transition" data-routine-id="${routine.id}" ${isCompleted ? 'disabled' : ''}>
            ${isCompleted ? '✓ Feito' : 'Concluir'}
          </button>
        </div>
      </div>
    `;
  }).join('');
}

// =============================================================================
// 4. MUTAÇÕES OTIMISTAS COM ROLLBACK IMEDIATO (SPEC-004)
// =============================================================================

/**
 * Mutação Otimista: Concluir Tarefa Acadêmica
 * 1. Salva snapshot do card e métricas anteriores.
 * 2. Atualiza o DOM imediatamente (estilo concluído, decrementa contadores).
 * 3. Envia POST /api/tasks/{id}/complete em segundo plano.
 * 4. Em caso de erro, reverte imediatamente o DOM (rollback) e exibe toast de erro.
 */
async function handleOptimisticTaskComplete(taskId, cardElement) {
  if (!cardElement) return;

  // 1. Snapshot do estado
  const cardSnapshotHtml = cardElement.outerHTML;
  const previousMetrics = { ...AppState.metrics };
  const taskIndex = AppState.reading_queue.findIndex((t) => t.task_id === taskId);
  const taskObj = taskIndex !== -1 ? { ...AppState.reading_queue[taskIndex] } : null;

  // 2. Atualização otimista no DOM
  cardElement.classList.add('task-completed-anim');
  const completeBtn = cardElement.querySelector('.btn-complete');
  if (completeBtn) {
    completeBtn.disabled = true;
    completeBtn.innerHTML = 'Gravando...';
  }

  if (taskObj && taskObj.status !== 'COMPLETED') {
    AppState.metrics.totalStudyMin = Math.max(0, AppState.metrics.totalStudyMin - (taskObj.est_duration_min || 0));
    AppState.metrics.totalPendingPages = Math.max(0, AppState.metrics.totalPendingPages - (taskObj.page_count || 0));
    AppState.metrics.completedTasksCount += 1;
    renderHeaderStatus();
  }

  // 3. Disparo assíncrono em background
  try {
    const response = await fetch(`/api/tasks/${encodeURIComponent(taskId)}/complete`, {
      method: 'POST'
    });

    if (!response.ok) {
      throw new Error(`Servidor retornou status HTTP ${response.status}`);
    }

    const result = await response.json();
    if (taskIndex !== -1) {
      AppState.reading_queue[taskIndex].status = 'COMPLETED';
    }

    // Sucesso definitivo
    renderReadingQueue();
    recalculateMetrics();
    renderHeaderStatus();
    showToast(`Tarefa '${taskObj ? taskObj.title : taskId}' concluída com sucesso!`, 'success');
  } catch (error) {
    console.error('[Rollback] Falha na mutação otimista de conclusão:', error);
    // 4. Rollback imediato
    AppState.metrics = previousMetrics;
    renderHeaderStatus();

    // Restaura o elemento original no DOM
    const tempDiv = document.createElement('div');
    tempDiv.innerHTML = cardSnapshotHtml;
    const restoredCard = tempDiv.firstElementChild;
    cardElement.replaceWith(restoredCard);

    showToast(`Erro ao concluir tarefa. Alteração revertida: ${error.message}`, 'error');
  }
}

/**
 * Mutação Otimista: Adiar Tarefa em 1 hora (Snooze 60 min)
 * 1. Feedback visual imediato no botão.
 * 2. POST /api/tasks/{id}/snooze?min=60
 * 3. Trata conflito 409 de janela saturada ou erro de rede.
 */
async function handleOptimisticTaskSnooze(taskId, buttonElement, cardElement) {
  if (!buttonElement) return;

  const originalText = buttonElement.innerHTML;
  buttonElement.disabled = true;
  buttonElement.innerHTML = `
    <svg class="w-4 h-4 animate-spin text-amber-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
      <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
    </svg>
    Recalculando...
  `;

  try {
    const response = await fetch(`/api/tasks/${encodeURIComponent(taskId)}/snooze?min=60`, {
      method: 'POST'
    });

    if (response.status === 409) {
      const errJson = await response.json().catch(() => ({}));
      const detailMsg = errJson.detail || 'Janela saturada para respeitar margem de 24h.';
      throw new Error(`Conflito: ${detailMsg}`);
    }

    if (!response.ok) {
      throw new Error(`HTTP ${response.status}: Falha ao adiar tarefa.`);
    }

    const result = await response.json();
    showToast('Tarefa adiada em 1h com sucesso! Reagendando blocos...', 'success');

    // Atualiza o painel inteiro para renderizar os novos horários alocados na grelha
    await fetchDailyDashboard();
  } catch (error) {
    console.warn('[Snooze] Falha ou conflito:', error);
    buttonElement.disabled = false;
    buttonElement.innerHTML = originalText;
    showToast(error.message, 'warning');
  }
}

/**
 * Mutação Otimista: Concluir Rotina Doméstica
 * 1. Snapshot da rotina no card.
 * 2. Marca como feito hoje de imediato.
 * 3. POST /api/routines/{id}/complete
 * 4. Rollback em caso de falha.
 */
async function handleOptimisticRoutineComplete(routineId, cardElement, buttonElement) {
  if (!cardElement || !buttonElement) return;

  const cardSnapshotHtml = cardElement.outerHTML;
  const routineIndex = AppState.domestic_routines.findIndex((r) => r.id === Number(routineId));
  const previousObj = routineIndex !== -1 ? { ...AppState.domestic_routines[routineIndex] } : null;

  // Atualização otimista
  buttonElement.disabled = true;
  buttonElement.className = 'btn-routine-complete touch-target px-3 py-1.5 rounded-lg bg-slate-800 text-slate-500 cursor-not-allowed font-semibold text-xs transition';
  buttonElement.textContent = '✓ Feito';

  const badge = cardElement.querySelector('.routine-status-badge');
  if (badge) {
    badge.className = 'routine-status-badge text-[10px] px-2 py-0.5 rounded-md font-semibold bg-emerald-950 text-emerald-400 border border-emerald-800';
    badge.textContent = 'Feito hoje';
  }

  const titleEl = cardElement.querySelector('h3');
  if (titleEl) {
    titleEl.classList.add('line-through', 'text-slate-400');
  }

  AppState.metrics.completedTasksCount += 1;
  renderHeaderStatus();

  try {
    const response = await fetch(`/api/routines/${encodeURIComponent(routineId)}/complete`, {
      method: 'POST'
    });

    if (!response.ok) {
      throw new Error(`HTTP ${response.status}: Erro ao concluir rotina.`);
    }

    const result = await response.json();
    if (routineIndex !== -1) {
      AppState.domestic_routines[routineIndex].is_completed_today = true;
      AppState.domestic_routines[routineIndex].last_completed = result.last_completed;
    }

    showToast('Rotina doméstica marcada como concluída!', 'success');
  } catch (error) {
    console.error('[Rollback] Falha ao concluir rotina doméstica:', error);

    // Rollback
    AppState.metrics.completedTasksCount = Math.max(0, AppState.metrics.completedTasksCount - 1);
    renderHeaderStatus();

    const tempDiv = document.createElement('div');
    tempDiv.innerHTML = cardSnapshotHtml;
    cardElement.replaceWith(tempDiv.firstElementChild);

    showToast(`Falha ao registrar rotina: ${error.message}`, 'error');
  }
}

// =============================================================================
// 5. TOAST NOTIFICATIONS & UTILITÁRIOS
// =============================================================================

function showToast(message, type = 'info') {
  const container = document.getElementById('toast-container');
  if (!container) return;

  const toast = document.createElement('div');
  const typeStyles = {
    success: 'bg-emerald-950 text-emerald-200 border-emerald-700/80 shadow-emerald-950/40',
    error: 'bg-rose-950 text-rose-200 border-rose-700/80 shadow-rose-950/40',
    warning: 'bg-amber-950 text-amber-200 border-amber-700/80 shadow-amber-950/40',
    info: 'bg-slate-800 text-slate-200 border-slate-700 shadow-slate-950/40'
  };

  const icons = {
    success: '✓',
    error: '✕',
    warning: '⚠️',
    info: 'ℹ️'
  };

  toast.className = `pointer-events-auto p-3.5 rounded-xl border shadow-lg text-xs font-medium flex items-center justify-between gap-3 transition-all duration-300 transform translate-y-2 opacity-0 ${typeStyles[type] || typeStyles.info}`;
  toast.innerHTML = `
    <div class="flex items-center gap-2">
      <span class="text-sm shrink-0">${icons[type] || '•'}</span>
      <span>${escapeHtml(message)}</span>
    </div>
    <button class="text-slate-400 hover:text-white shrink-0 p-1" onclick="this.parentElement.remove()">✕</button>
  `;

  container.appendChild(toast);

  // Animação de entrada
  requestAnimationFrame(() => {
    toast.classList.remove('translate-y-2', 'opacity-0');
  });

  // Auto-remoção após 4 segundos
  setTimeout(() => {
    toast.classList.add('opacity-0', 'translate-y-2');
    setTimeout(() => toast.remove(), 300);
  }, 4000);
}

function formatTime(isoString) {
  if (!isoString) return '--:--';
  try {
    const d = new Date(isoString);
    if (isNaN(d.getTime())) return isoString.slice(11, 16) || isoString;
    return d.toLocaleTimeString('pt-BR', { hour: '2-digit', minute: '2-digit' });
  } catch {
    return isoString;
  }
}

function formatDueDateTime(isoString) {
  if (!isoString) return 'Sem prazo';
  try {
    const d = new Date(isoString);
    if (isNaN(d.getTime())) return isoString;
    return d.toLocaleDateString('pt-BR', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
  } catch {
    return isoString;
  }
}

function escapeHtml(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}
