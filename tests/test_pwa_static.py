"""
test_pwa_static.py
Validação automatizada da infraestrutura PWA, entrega de arquivos estáticos,
contratos do Manifesto W3C, Service Worker e integridade das 3 secções do App Shell (SPEC-004).
"""

from pathlib import Path
import pytest
from httpx import AsyncClient, ASGITransport

from backend.app.main import app, STATIC_DIR


@pytest.fixture
async def pwa_client():
    """Cliente HTTP assíncrono para validação da entrega de arquivos estáticos e PWA."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


@pytest.mark.anyio
async def test_root_index_html_delivery(pwa_client: AsyncClient):
    """Garante que GET / serve index.html (200 OK, text/html) com os containers estruturais requeridos."""
    response = await pwa_client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers.get("content-type", "")

    html = response.text
    # 1. Metadados e links do PWA
    assert '<link rel="manifest" href="/manifest.json">' in html
    assert '/static/app.js' in html

    # 2. Containers estruturais das 3 secções obrigatórias
    assert 'id="daily-schedule-section"' in html
    assert 'id="reading-queue-section"' in html
    assert 'id="domestic-routines-section"' in html

    # 3. Componentes essenciais de métricas e status
    assert 'id="connection-status"' in html
    assert 'id="total-study-time"' in html
    assert 'id="total-pending-pages"' in html
    assert 'id="total-completed-tasks"' in html
    assert 'id="toast-container"' in html


@pytest.mark.anyio
async def test_manifest_json_specification(pwa_client: AsyncClient):
    """Garante que GET /manifest.json retorna 200 OK e JSON W3C PWA compatível."""
    response = await pwa_client.get("/manifest.json")
    assert response.status_code == 200
    assert "application/manifest+json" in response.headers.get("content-type", "")

    manifest = response.json()
    assert manifest["name"] == "Assistente Académico & Agendamento Preditivo"
    assert manifest["short_name"] == "AcadAgent"
    assert manifest["start_url"] == "/"
    assert manifest["scope"] == "/"
    assert manifest["display"] == "standalone"
    assert "background_color" in manifest
    assert "theme_color" in manifest

    icons = manifest.get("icons", [])
    assert len(icons) >= 2
    sizes = [icon.get("sizes") for icon in icons]
    assert "192x192" in sizes
    assert "512x512" in sizes
    purposes = [icon.get("purpose") for icon in icons]
    assert any("maskable" in str(p) for p in purposes if p)


@pytest.mark.anyio
async def test_service_worker_delivery_and_headers(pwa_client: AsyncClient):
    """Garante que GET /service-worker.js retorna 200 OK, Service-Worker-Allowed: / e JavaScript válido."""
    response = await pwa_client.get("/service-worker.js")
    assert response.status_code == 200
    assert "javascript" in response.headers.get("content-type", "")
    assert response.headers.get("Service-Worker-Allowed") == "/"

    sw_content = response.text
    assert "caches.open" in sw_content
    assert "acadagent-shell" in sw_content
    assert "addEventListener('install'" in sw_content
    assert "addEventListener('activate'" in sw_content
    assert "addEventListener('fetch'" in sw_content
    # Valida estratégias exigidas: network-first para /api/dashboard/daily
    assert "/api/dashboard/daily" in sw_content


@pytest.mark.anyio
async def test_static_files_mount_and_app_js(pwa_client: AsyncClient):
    """Garante que ativos estáticos são montados em /static e app.js é servido com sucesso."""
    # 1. app.js
    response = await pwa_client.get("/static/app.js")
    assert response.status_code == 200
    assert "javascript" in response.headers.get("content-type", "")
    app_js = response.text
    assert "fetchDailyDashboard" in app_js
    assert "handleOptimisticTaskComplete" in app_js
    assert "handleOptimisticTaskSnooze" in app_js
    assert "handleOptimisticRoutineComplete" in app_js

    # 2. index.html via /static/index.html
    response_idx = await pwa_client.get("/static/index.html")
    assert response_idx.status_code == 200
    assert "text/html" in response_idx.headers.get("content-type", "")

    # 3. Ícones PWA
    response_svg = await pwa_client.get("/static/icons/icon.svg")
    assert response_svg.status_code == 200

    response_192 = await pwa_client.get("/static/icons/icon-192.png")
    assert response_192.status_code == 200
    assert response_192.headers.get("content-type") == "image/png"


@pytest.mark.anyio
async def test_all_precache_and_manifest_assets_exist_no_404(pwa_client: AsyncClient):
    """Regressão QA (Camada E.3): Garante que 100% dos arquivos listados em APP_SHELL_ASSETS
    do service-worker.js e em icons do manifest.json respondem 200 OK (evitando aborto do cache.addAll)."""
    import re

    sw_resp = await pwa_client.get("/service-worker.js")
    assert sw_resp.status_code == 200
    sw_text = sw_resp.text

    # Extrai array APP_SHELL_ASSETS do service-worker.js
    match = re.search(r"APP_SHELL_ASSETS\s*=\s*\[(.*?)\]", sw_text, re.DOTALL)
    assert match is not None
    assets = re.findall(r"['\"]([^'\"]+)['\"]", match.group(1))
    assert len(assets) >= 6

    for asset_url in assets:
        r = await pwa_client.get(asset_url)
        assert r.status_code == 200, f"Ativo {asset_url} do Service Worker retornou {r.status_code}"

    manifest_resp = await pwa_client.get("/manifest.json")
    manifest_data = manifest_resp.json()
    for icon in manifest_data.get("icons", []):
        icon_src = icon["src"]
        r_icon = await pwa_client.get(icon_src)
        assert r_icon.status_code == 200, f"Ícone {icon_src} do manifest.json retornou {r_icon.status_code}"

