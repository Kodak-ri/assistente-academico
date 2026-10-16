from datetime import datetime
from typing import Optional, List
from sqlalchemy import String, Boolean, DateTime, Integer, Float, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.core.database import Base


class Course(Base):
    __tablename__ = "courses"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    materials: Mapped[List["Material"]] = relationship(back_populates="course", cascade="all, delete-orphan")
    tasks: Mapped[List["Task"]] = relationship(back_populates="course", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<Course(id='{self.id}', name='{self.name}', is_active={self.is_active})>"


class Material(Base):
    __tablename__ = "materials"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    course_id: Mapped[str] = mapped_column(String(64), ForeignKey("courses.id"), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    page_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    lexical_density: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)

    course: Mapped["Course"] = relationship(back_populates="materials")

    def __repr__(self) -> str:
        return f"<Material(id='{self.id}', course_id='{self.course_id}', pages={self.page_count})>"


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    course_id: Mapped[str] = mapped_column(String(64), ForeignKey("courses.id"), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    due_datetime: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    est_duration_min: Mapped[int] = mapped_column(Integer, nullable=False)
    group_required: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="PENDING", nullable=False)

    course: Mapped["Course"] = relationship(back_populates="tasks")
    calendar_allocations: Mapped[List["CalendarAllocation"]] = relationship(back_populates="task", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<Task(id='{self.id}', title='{self.title}', status='{self.status}')>"


class CalendarAllocation(Base):
    __tablename__ = "calendar_allocations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(64), ForeignKey("tasks.id"), nullable=False)
    gcal_event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    start_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    end_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    task: Mapped["Task"] = relationship(back_populates="calendar_allocations")

    def __repr__(self) -> str:
        return f"<CalendarAllocation(id={self.id}, task_id='{self.task_id}', gcal_id='{self.gcal_event_id}')>"


class DomesticRoutine(Base):
    __tablename__ = "domestic_routines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    cron_expression: Mapped[str] = mapped_column(String(64), nullable=False)
    last_completed: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    def __repr__(self) -> str:
        return f"<DomesticRoutine(id={self.id}, name='{self.name}', cron='{self.cron_expression}')>"
