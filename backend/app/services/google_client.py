import io
import os
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from sqlalchemy.orm import Session

from backend.app.models.db_models import Course

SCOPES = [
    "https://www.googleapis.com/auth/classroom.courses.readonly",
    "https://www.googleapis.com/auth/classroom.coursework.me.readonly",
    "https://www.googleapis.com/auth/classroom.announcements.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/calendar",
]


def parse_rfc3339_datetime(dt_str: Optional[str]) -> Optional[datetime]:
    """Converte strings RFC 3339 / ISO 8601 do Google Workspace para datetime UTC."""
    if not dt_str:
        return None
    try:
        # Normaliza 'Z' para '+00:00' para suporte no fromisoformat
        clean_str = dt_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


class GoogleWorkspaceClient:
    """Cliente unificado para Google Classroom, Drive e Calendar com triagem tripartida."""

    def __init__(
        self,
        token_path: Optional[str] = None,
        credentials_path: Optional[str] = None,
        scopes: Optional[List[str]] = None,
        credentials: Optional[Credentials] = None,
        classroom_service=None,
        drive_service=None,
        calendar_service=None,
    ):
        self.token_path = token_path or os.getenv("GOOGLE_TOKEN_PATH", "token.json")
        self.credentials_path = credentials_path or os.getenv("GOOGLE_CREDENTIALS_PATH", "credentials.json")
        self.scopes = scopes or SCOPES
        self.credentials = credentials
        self.classroom_service = classroom_service
        self.drive_service = drive_service
        self.calendar_service = calendar_service

        if self.credentials:
            self._init_services_from_creds()

    def _init_services_from_creds(self):
        if not self.classroom_service:
            self.classroom_service = build("classroom", "v1", credentials=self.credentials)
        if not self.drive_service:
            self.drive_service = build("drive", "v3", credentials=self.credentials)
        if not self.calendar_service:
            self.calendar_service = build("calendar", "v3", credentials=self.credentials)

    def authenticate(self) -> None:
        """Carrega credenciais salvas, renova refresh_token preventivamente ou inicia fluxo OAuth2."""
        if self.credentials and self.credentials.valid:
            self._init_services_from_creds()
            return

        if self.token_path and os.path.exists(self.token_path):
            self.credentials = Credentials.from_authorized_user_file(self.token_path, self.scopes)

        # Renovação preventiva do refresh_token
        if self.credentials and self.credentials.expired and self.credentials.refresh_token:
            self.credentials.refresh(Request())
            self._save_token()
        elif not self.credentials or not self.credentials.valid:
            if self.credentials_path and os.path.exists(self.credentials_path):
                flow = InstalledAppFlow.from_client_secrets_file(self.credentials_path, self.scopes)
                self.credentials = flow.run_local_server(port=0)
                self._save_token()
            else:
                raise FileNotFoundError(
                    f"Ficheiro de credenciais '{self.credentials_path}' não encontrado para autenticação inicial."
                )

        self._init_services_from_creds()

    def _save_token(self) -> None:
        """Persiste as credenciais renovadas em disco."""
        if self.credentials and self.token_path:
            with open(self.token_path, "w", encoding="utf-8") as token_file:
                token_file.write(self.credentials.to_json())

    def filter_active_courses(
        self,
        db_session: Session,
        reference_date: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        """Aplica os três filtros determinísticos sequenciais para isolar as turmas ativas do semestre:
        
        1. Filtro de Criação: Descarte com creationTime > 180 dias (exceto se is_active=True no banco).
        2. Filtro de Persistência Local: Se is_active=False no banco, ignore imediatamente.
        3. Filtro de Atividade no Mural/Tarefas: Descarte se sem publicações nos últimos 45 dias.
        """
        ref_dt = reference_date or datetime.now(timezone.utc)
        if ref_dt.tzinfo is None:
            ref_dt = ref_dt.replace(tzinfo=timezone.utc)

        if not self.classroom_service:
            raise RuntimeError("Classroom service não inicializado. Execute authenticate() primeiro.")

        response = self.classroom_service.courses().list(courseStates=["ACTIVE"]).execute()
        courses_raw = response.get("courses", [])

        active_courses: List[Dict[str, Any]] = []

        for course in courses_raw:
            course_id = str(course.get("id"))
            course_name = course.get("name", "Sem Nome")

            # Consulta estado na base de dados local
            local_record = db_session.query(Course).filter(Course.id == course_id).first()

            # ----------------------------------------------------
            # FILTRO 2: Persistência Local
            # Se a turma existir na tabela courses com is_active=False, ignore-a imediatamente.
            # ----------------------------------------------------
            if local_record and not local_record.is_active:
                continue

            # ----------------------------------------------------
            # FILTRO 1: Criação (> 180 dias)
            # Descarte se > 180 dias (a menos que já conste explicitamente com is_active=True)
            # ----------------------------------------------------
            creation_str = course.get("creationTime")
            creation_dt = parse_rfc3339_datetime(creation_str)

            already_explicitly_active = (local_record is not None and local_record.is_active is True)

            if creation_dt:
                age_days = (ref_dt - creation_dt).total_seconds() / 86400.0
                if age_days > 180 and not already_explicitly_active:
                    continue

            # ----------------------------------------------------
            # FILTRO 3: Atividade no Mural/Tarefas nos últimos 45 dias
            # Consulte announcements.list e courseWork.list
            # ----------------------------------------------------
            has_recent_activity = False
            threshold_dt = ref_dt - timedelta(days=45)

            # Checar announcements
            ann_resp = self.classroom_service.courses().announcements().list(courseId=course_id).execute()
            for ann in ann_resp.get("announcements", []):
                ann_dt = parse_rfc3339_datetime(ann.get("updateTime") or ann.get("creationTime"))
                if ann_dt and ann_dt >= threshold_dt:
                    has_recent_activity = True
                    break

            # Checar courseWork se ainda não encontrou atividade
            if not has_recent_activity:
                cw_resp = self.classroom_service.courses().courseWork().list(courseId=course_id).execute()
                for cw in cw_resp.get("courseWork", []):
                    cw_dt = parse_rfc3339_datetime(cw.get("updateTime") or cw.get("creationTime"))
                    if cw_dt and cw_dt >= threshold_dt:
                        has_recent_activity = True
                        break

            if not has_recent_activity:
                # Classifique a turma como dormente (is_active=False)
                if local_record:
                    local_record.is_active = False
                    local_record.last_synced_at = ref_dt
                else:
                    dormant_course = Course(
                        id=course_id,
                        name=course_name,
                        is_active=False,
                        last_synced_at=ref_dt,
                    )
                    db_session.add(dormant_course)
                db_session.commit()
                continue

            # Turma ativa aprovada em todos os filtros
            if local_record:
                local_record.is_active = True
                local_record.name = course_name
                local_record.last_synced_at = ref_dt
            else:
                new_course = Course(
                    id=course_id,
                    name=course_name,
                    is_active=True,
                    last_synced_at=ref_dt,
                )
                db_session.add(new_course)
            db_session.commit()

            active_courses.append(course)

        return active_courses

    def extract_pdf_attachments(self, course_id: str) -> List[Dict[str, Any]]:
        """Identifica anexos do Google Drive que sejam ficheiros PDF em atividades e anúncios."""
        if not self.classroom_service:
            raise RuntimeError("Classroom service não inicializado.")

        pdf_items: List[Dict[str, Any]] = []

        # 1. Varre atividades (courseWork)
        cw_resp = self.classroom_service.courses().courseWork().list(courseId=course_id).execute()
        for cw in cw_resp.get("courseWork", []):
            cw_id = str(cw.get("id"))
            cw_title = cw.get("title", "Sem título")
            
            # Formatação do due_datetime se presente
            due_date = cw.get("dueDate")
            due_time = cw.get("dueTime")
            due_dt = None
            if due_date and "year" in due_date:
                hour = due_time.get("hours", 23) if due_time else 23
                minute = due_time.get("minutes", 59) if due_time else 59
                due_dt = datetime(
                    year=due_date["year"],
                    month=due_date["month"],
                    day=due_date["day"],
                    hour=hour,
                    minute=minute,
                    tzinfo=timezone.utc,
                )

            for material in cw.get("materials", []):
                drive_file_wrapper = material.get("driveFile")
                if drive_file_wrapper:
                    df = drive_file_wrapper.get("driveFile", {})
                    file_id = df.get("id")
                    title = df.get("title", "")
                    if file_id and (title.lower().endswith(".pdf") or "pdf" in df.get("alternateLink", "").lower()):
                        pdf_items.append({
                            "type": "courseWork",
                            "course_id": course_id,
                            "work_id": cw_id,
                            "work_title": cw_title,
                            "file_id": file_id,
                            "file_title": title,
                            "due_datetime": due_dt,
                        })

        # 2. Varre anúncios (announcements)
        ann_resp = self.classroom_service.courses().announcements().list(courseId=course_id).execute()
        for ann in ann_resp.get("announcements", []):
            ann_id = str(ann.get("id"))
            ann_text = ann.get("text", "Anúncio")
            for material in ann.get("materials", []):
                drive_file_wrapper = material.get("driveFile")
                if drive_file_wrapper:
                    df = drive_file_wrapper.get("driveFile", {})
                    file_id = df.get("id")
                    title = df.get("title", "")
                    if file_id and (title.lower().endswith(".pdf") or "pdf" in df.get("alternateLink", "").lower()):
                        pdf_items.append({
                            "type": "announcement",
                            "course_id": course_id,
                            "work_id": ann_id,
                            "work_title": ann_text[:100],
                            "file_id": file_id,
                            "file_title": title,
                            "due_datetime": None,
                        })

        return pdf_items

    def download_drive_pdf_to_memory(self, file_id: str) -> io.BytesIO:
        """Faz download do conteúdo binário de um PDF diretamente para io.BytesIO sem gravar em disco."""
        if not self.drive_service:
            raise RuntimeError("Drive service não inicializado.")

        request = self.drive_service.files().get_media(fileId=file_id)
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            status, done = downloader.next_chunk()
        fh.seek(0)
        return fh


_cached_client: Optional[GoogleWorkspaceClient] = None


def get_google_workspace_client() -> Optional[GoogleWorkspaceClient]:
    """Retorna o cliente Google Workspace autenticado ou None se não configurado."""
    global _cached_client
    if _cached_client is not None:
        return _cached_client
    try:
        client = GoogleWorkspaceClient()
        client.authenticate()
        _cached_client = client
        return client
    except Exception:
        return None


def get_calendar_service():
    """Retorna o serviço Google Calendar v3 autenticado ou None."""
    client = get_google_workspace_client()
    return client.calendar_service if client else None


def get_classroom_service():
    """Retorna o serviço Google Classroom v1 autenticado ou None."""
    client = get_google_workspace_client()
    return client.classroom_service if client else None


def get_drive_service():
    """Retorna o serviço Google Drive v3 autenticado ou None."""
    client = get_google_workspace_client()
    return client.drive_service if client else None
