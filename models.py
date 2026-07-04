"""
models.py
---------
Pydantic models for the University Course Scheduler API.
"""

from pydantic import BaseModel
from typing import List, Optional, Any


class InstanceEntry(BaseModel):
    """One schedulable session (theory period or lab session)."""
    course_id:     int
    course_code:   str = ""
    course_type:   str            # 'theory' or 'lab'
    duration:      int            # minutes: 50 for theory, 150 for lab
    student_group: int            # index into groups list
    faculty:       int            # faculty index (-1 = unassigned)


class CourseEntry(BaseModel):
    """Course metadata (for display and reference)."""
    id:      int
    code:    str = ""
    type:    str = "theory"
    credits: float = 3.0
    sg:      int = 0


class SchedulerInput(BaseModel):
    """
    Full input payload from the web form.
    Contains explicit instances (sessions) with course/faculty/group assigned.
    """
    # Explicit data
    groups:           List[str]          = []
    courses:          List[CourseEntry]  = []
    instances:        List[InstanceEntry]= []

    # Room config — theory classrooms and lab rooms are separate pools
    n_classrooms:     int   = 4    # number of theory classrooms (R-301, R-302 ...)
    n_labs:           int   = 6    # number of lab rooms (Lab-1, Lab-2 ...)
    n_student_groups: int   = 1
    seed:             int   = 42

    # Constraint 18 — Daily Primary Classroom: home_classrooms[s] = P_s, the
    # fixed theory classroom index (0-based into the n_classrooms pool) for
    # student group s. -1 (or missing entry) means "no home room" — group s
    # is left fully flexible, same as before this constraint existed.
    home_classrooms: List[int] = []

    # NSGA-II hyperparameters
    pop_size: int   = 80
    n_gen:    int   = 100

    # Objective weights
    w1: float = 0.4
    w2: float = 0.3
    w3: float = 0.3
    P:  float = 0.02


class ParetoSolution(BaseModel):
    """One solution on the Pareto front."""
    index: int
    C_max: float
    W_max: float
    U_max: float
    Idle:  float


class ScheduleEntry(BaseModel):
    """One scheduled class instance."""
    instance_id:    int
    course_id:      int
    course_code:    str = ""
    course_type:    str
    classroom:      int    # index into the relevant pool (theory or lab)
    room_label:     str = ""   # human-readable: "R-301" or "Lab-3"
    day:            int
    slot_start:     int
    start_min:      int
    end_min:        int
    duration:       int
    faculty:        int
    student_groups: List[int]


class SolverResult(BaseModel):
    """Full result returned to the frontend after solving."""
    status:           str
    solve_time:       Optional[float]      = None
    pareto_solutions: List[ParetoSolution] = []
    best_schedule:    List[ScheduleEntry]  = []
    all_schedules:    List[List[ScheduleEntry]] = []   # one schedule per Pareto solution, indexed the same way
    n_instances:      int   = 0
    n_theory_courses: int   = 0
    n_lab_courses:    int   = 0
    n_classrooms:     int   = 0
    n_labs:           int   = 0
    n_faculty:        int   = 0
    n_student_groups: int   = 0
    groups:           List[str] = []
    courses:          List[Any] = []
    DAYS:             int   = 5
    Td:               int   = 540