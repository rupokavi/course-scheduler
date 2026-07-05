"""
solver.py
---------
NSGA-II based multi-objective university course scheduler.
Theory sessions use classroom pool (R-301...). Lab sessions use lab pool (Lab-1...).
Implements all thesis constraints C1-C6, C15-C18.
"""

import time
import numpy as np

try:
    from pymoo.core.problem import Problem
    from pymoo.algorithms.moo.nsga2 import NSGA2
    from pymoo.operators.crossover.sbx import SBX
    from pymoo.operators.mutation.pm import PM
    from pymoo.operators.sampling.rnd import FloatRandomSampling
    from pymoo.termination import get_termination
    from pymoo.optimize import minimize as pymoo_minimize
    PYMOO_AVAILABLE = True
except ImportError:
    PYMOO_AVAILABLE = False
    class Problem:
        pass

from models import SchedulerInput, SolverResult, ParetoSolution, ScheduleEntry


# ══════════════════════════════════════════════════════════════════════════════
#  SECTION 1 — INSTANCE BUILDER
# ══════════════════════════════════════════════════════════════════════════════

def build_instance(inp: SchedulerInput) -> dict:
    instances    = inp.instances
    n_instances  = len(instances)
    n_classrooms = inp.n_classrooms   # theory rooms only
    n_labs       = inp.n_labs         # lab rooms only
    n_groups     = max(inp.n_student_groups, len(inp.groups), 1)

    all_fac   = [inst.faculty for inst in instances if inst.faculty >= 0]
    n_faculty = (max(all_fac) + 1) if all_fac else 1

    theory_courses = set(inst.course_id for inst in instances if inst.course_type == 'theory')
    lab_courses    = set(inst.course_id for inst in instances if inst.course_type == 'lab')

    durations         = [inst.duration     for inst in instances]
    course_of_inst    = [inst.course_id    for inst in instances]
    instance_type     = [inst.course_type  for inst in instances]
    faculty_of_inst   = [inst.faculty      for inst in instances]
    course_code_of    = [inst.course_code  for inst in instances]
    student_groups_of = [[inst.student_group] for inst in instances]

    BREAK1          = (150, 170)
    BREAK2          = (320, 390)
    THEORY_RESTRICT = (390, 540)
    Td   = 540
    DAYS = 5

    # C15: theory allowed start slots (slot index = minutes / 10)
    # 8:00→0, 8:50→5, 9:40→10, 10:50→17, 11:40→22, 12:30→27
    L_THEORY_SLOT = {0, 5, 10, 17, 22, 27}

    # C16: lab allowed start slots
    # 8:00→0, 10:50→17, 2:30→39
    L_LAB_SLOT = {0, 17, 39}

    # Total combined rooms for U_max objective:
    # index 0..(n_classrooms-1)  → theory classrooms
    # index n_classrooms..(n_classrooms+n_labs-1) → lab rooms
    n_rooms_total = n_classrooms + n_labs

    # Constraint 18 — Daily Primary Classroom: P_s per student group.
    # -1 = no home room configured for that group (fully flexible, legacy behaviour).
    home_classroom_of_group = [-1] * n_groups
    for s, p in enumerate(inp.home_classrooms):
        if s < n_groups and 0 <= p < n_classrooms:
            home_classroom_of_group[s] = p

    return dict(
        n_instances         = n_instances,
        n_classrooms        = n_classrooms,
        n_labs              = n_labs,
        n_rooms_total       = n_rooms_total,
        n_faculty           = n_faculty,
        n_student_groups    = n_groups,
        n_theory_courses    = len(theory_courses),
        n_lab_courses       = len(lab_courses),
        durations           = durations,
        course_of_instance  = course_of_inst,
        instance_type       = instance_type,
        faculty_of_instance = faculty_of_inst,
        student_groups_of   = student_groups_of,
        course_code_of      = course_code_of,
        Td                  = Td,
        DAYS                = DAYS,
        BREAK1              = BREAK1,
        BREAK2              = BREAK2,
        THEORY_RESTRICT     = THEORY_RESTRICT,
        L_THEORY_SLOT       = L_THEORY_SLOT,
        L_LAB_SLOT          = L_LAB_SLOT,
        home_classroom_of_group = home_classroom_of_group,
    )


# ══════════════════════════════════════════════════════════════════════════════
#  SECTION 2 — GREEDY DECODER
#  Key change: theory sessions pick from classroom pool [0..n_classrooms-1]
#              lab sessions pick from lab pool [0..n_labs-1] separately.
#              room_busy tracks both pools with offset:
#                combined index = class_room_idx  (theory)
#                combined index = n_classrooms + lab_room_idx  (lab)
# ══════════════════════════════════════════════════════════════════════════════

def _greedy_decode(x: np.ndarray, inst: dict) -> dict:
    """
    schedule[i] = (room_combined_idx, day, slot_start, faculty, student_groups)
    room_combined_idx:
      0 .. n_classrooms-1        → theory classroom r
      n_classrooms .. total-1    → lab room (idx - n_classrooms)
    """
    m           = inst["n_instances"]
    n_class     = inst["n_classrooms"]
    n_labs      = inst["n_labs"]
    n_rooms     = inst["n_rooms_total"]
    D           = inst["DAYS"]
    Td          = inst["Td"]
    dur         = inst["durations"]
    itype       = inst["instance_type"]
    F_count     = inst["n_faculty"]
    S_count     = inst["n_student_groups"]
    fac_of      = inst["faculty_of_instance"]
    stg_of      = inst["student_groups_of"]
    coi         = inst["course_of_instance"]
    B1          = inst["BREAK1"]
    B2          = inst["BREAK2"]
    TR          = inst["THEORY_RESTRICT"]
    L_THEORY    = inst["L_THEORY_SLOT"]
    L_LAB       = inst["L_LAB_SLOT"]
    home_of     = inst["home_classroom_of_group"]   # C18: P_s per student group, -1 = unconstrained

    SLOT   = 10
    slotsD = Td // SLOT

    priorities = x[0:m]
    room_pref  = x[m:2*m]
    day_pref   = x[2*m:3*m]
    order      = np.argsort(priorities)

    # Combined room busy list (theory rooms + lab rooms together)
    room_busy = [[[] for _ in range(D)] for _ in range(n_rooms)]
    fac_busy  = [[[] for _ in range(D)] for _ in range(F_count)]
    stg_busy  = [[[] for _ in range(D)] for _ in range(S_count)]

    lab_count        = [[0] * D for _ in range(S_count)]  # C17
    course_day_count = {}                                   # C18
    theory_daily_count = [[0] * D for _ in range(S_count)]  # C18 (new): N_sd, theory sessions of group s placed so far on day d

    schedule = {}

    def overlaps(busy_list, ts, te):
        return any(ts < b and te > a for a, b in busy_list)

    def spans_break(t, ds):
        t_start, t_end = t * SLOT, (t + ds) * SLOT
        return any(t_start < be and t_end > bs for bs, be in [B1, B2])

    def in_restrict(t, is_theory):
        return is_theory and TR[0] <= t * SLOT < TR[1]

    for i in order:
        di        = dur[i]
        ds        = max(1, di // SLOT)
        fi        = fac_of[i]
        sis       = stg_of[i]
        is_theory = (itype[i] == 'theory')
        cid       = coi[i]

        # Choose the correct room pool
        pool_size  = n_class if is_theory else n_labs
        pool_offset = 0      if is_theory else n_class   # offset into combined list

        p_day  = int(day_pref[i]  * D)         % D
        p_room = int(room_pref[i] * pool_size)  % pool_size

        allowed_slots = sorted(L_THEORY if is_theory else L_LAB)

        placed = False
        for dd in range(D):
            d = (p_day + dd) % D

            # C18a — at most 1 session per course per day (pre-existing constraint)
            if course_day_count.get((cid, d), 0) >= 1:
                continue

            # Constraint 18 — Daily Primary Classroom.
            # For the first 3 theory sessions a group has on this day, the
            # room is FORCED to that group's home classroom P_s. Only the
            # 4th+ session that day is allowed to float to any theory room.
            s0      = sis[0] if sis else -1
            home_r  = home_of[s0] if (is_theory and 0 <= s0 < len(home_of)) else -1
            n_sd    = theory_daily_count[s0][d] if (is_theory and 0 <= s0 < S_count) else 0
            must_home = is_theory and home_r >= 0 and n_sd < 3

            if must_home:
                room_candidates = [home_r]                                   # locked to P_s
            else:
                room_candidates = [(p_room + rr) % pool_size for rr in range(pool_size)]  # free choice (incl. overflow)

            for r_local in room_candidates:
                r_combined = pool_offset + r_local   # index into room_busy

                for t in allowed_slots:
                    if t + ds > slotsD:
                        continue
                    if spans_break(t, ds):
                        continue
                    if in_restrict(t, is_theory):
                        continue

                    te = t + ds

                    # C2 — room non-overlap (within correct pool)
                    if overlaps(room_busy[r_combined][d], t, te):
                        continue

                    # C3 — faculty non-overlap
                    if fi >= 0 and overlaps(fac_busy[fi][d], t, te):
                        continue

                    # C4 — student group non-overlap
                    if any(overlaps(stg_busy[s][d], t, te) for s in sis):
                        continue

                    # C17 — at most 1 lab per student group per day
                    if not is_theory and any(lab_count[s][d] >= 1 for s in sis):
                        continue

                    # ── Place ──
                    room_busy[r_combined][d].append((t, te))
                    if fi >= 0:
                        fac_busy[fi][d].append((t, te))
                    for s in sis:
                        stg_busy[s][d].append((t, te))
                        if not is_theory:
                            lab_count[s][d] += 1
                        elif 0 <= s < S_count:
                            theory_daily_count[s][d] += 1   # Constraint 18: N_sd
                    course_day_count[(cid, d)] = course_day_count.get((cid, d), 0) + 1

                    # Store combined room index so we can recover pool later
                    schedule[i] = (r_combined, d, t, fi, sis)
                    placed = True
                    break
                if placed: break
            if placed: break

        if not placed:
            schedule[i] = (-1, 0, 0, fi, sis)

    return schedule


# ══════════════════════════════════════════════════════════════════════════════
#  SECTION 3 — NSGA-II PROBLEM CLASS
# ══════════════════════════════════════════════════════════════════════════════

class CourseFJSP(Problem):
    def __init__(self, inst, w1=0.4, w2=0.3, w3=0.3, P=0.02):
        self.inst = inst
        self.w1 = w1; self.w2 = w2; self.w3 = w3; self.P = P
        super().__init__(n_var=inst["n_instances"] * 3, n_obj=4, xl=0.0, xu=1.0)

    def _evaluate(self, X, out, *args, **kwargs):
        out["F"] = np.array([self._eval_one(x) for x in X])

    def _eval_one(self, x):
        inst    = self.inst
        m       = inst["n_instances"]
        D       = inst["DAYS"]
        Td      = inst["Td"]
        dur     = inst["durations"]
        S_count = inst["n_student_groups"]
        n_rooms = inst["n_rooms_total"]
        B1      = inst["BREAK1"]
        B2      = inst["BREAK2"]
        SLOT    = 10

        schedule = _greedy_decode(x, inst)
        stg_of   = inst["student_groups_of"]

        # C_max — real finish time of the placed schedule, plus a penalty that
        # SCALES with the number of unplaced sessions. The old version set
        # C_max flatly to D*Td (the absolute worst case) the moment even ONE
        # session was unplaced — which made 1 unplaced session look exactly
        # as bad as 10, gave the optimizer no gradient to reduce unplaced
        # count, and showed a Makespan number with no matching class in the
        # visible routine. This version keeps C_max tied to what's actually
        # on screen, with each unplaced session adding one extra "day" worth
        # of penalty on top.
        UNPLACED_PENALTY = Td   # one full day's worth of minutes, per unplaced session
        C_max = 0
        n_unplaced = 0
        for i in range(m):
            r, d, t = schedule[i][0], schedule[i][1], schedule[i][2]
            if r == -1:
                n_unplaced += 1
            else:
                C_max = max(C_max, d * Td + t * SLOT + dur[i])
        C_max += n_unplaced * UNPLACED_PENALTY

        # W_max
        W_max = 0
        for s in range(S_count):
            for d in range(D):
                w = sum(dur[i] for i in range(m)
                        if schedule[i][0] != -1
                        and schedule[i][1] == d
                        and s in stg_of[i])
                W_max = max(W_max, w)

        # U_max — across all rooms (theory + lab combined)
        U_max = 0
        for r in range(n_rooms):
            u = sum(dur[i] for i in range(m) if schedule[i][0] == r)
            U_max = max(U_max, u)

        # Idle gap
        idle_total = 0
        for s in range(S_count):
            for d in range(D):
                slots = [
                    (schedule[i][2] * SLOT, schedule[i][2] * SLOT + dur[i])
                    for i in range(m)
                    if schedule[i][0] != -1
                    and schedule[i][1] == d
                    and s in stg_of[i]
                ]
                if not slots: continue
                E_sd       = min(c[0] for c in slots)
                L_sd       = max(c[1] for c in slots)
                class_time = sum(c[1] - c[0] for c in slots)
                break_in   = sum(
                    max(0, min(be, L_sd) - max(bs, E_sd))
                    for bs, be in [B1, B2]
                )
                idle_total += max(0, L_sd - E_sd - class_time - break_in)

        return [float(C_max), float(W_max), float(U_max), float(idle_total)]


# ══════════════════════════════════════════════════════════════════════════════
#  SECTION 4 — MAIN SOLVE FUNCTION
# ══════════════════════════════════════════════════════════════════════════════

def _room_label(r_combined: int, n_classrooms: int) -> str:
    """Convert combined room index to human-readable label."""
    if r_combined < 0:
        return "Unplaced"
    if r_combined < n_classrooms:
        return f"R-{301 + r_combined}"          # R-301, R-302, ...
    else:
        lab_idx = r_combined - n_classrooms + 1
        return f"Lab-{lab_idx}"                  # Lab-1, Lab-2, ...


def _build_schedule_entries(x: np.ndarray, inst: dict) -> list:
    """Decode one decision vector into a list of ScheduleEntry objects."""
    schedule = _greedy_decode(x, inst)
    SLOT     = 10
    n_class  = inst["n_classrooms"]

    entries = []
    for i in range(inst["n_instances"]):
        r, d, t, fi, sis = schedule.get(i, (-1, 0, 0, -1, []))
        dur_i = inst["durations"][i]
        entries.append(ScheduleEntry(
            instance_id    = i,
            course_id      = inst["course_of_instance"][i],
            course_code    = inst["course_code_of"][i],
            course_type    = inst["instance_type"][i],
            classroom      = r,
            room_label     = _room_label(r, n_class),
            day            = d,
            slot_start     = t,
            start_min      = t * SLOT,
            end_min        = t * SLOT + dur_i,
            duration       = dur_i,
            faculty        = fi,
            student_groups = sis,
        ))
    return entries


def solve(inp: SchedulerInput) -> SolverResult:
    if not PYMOO_AVAILABLE:
        return SolverResult(status="error: pymoo not installed")
    if not inp.instances:
        return SolverResult(status="error: no instances provided")

    inst = build_instance(inp)

    problem = CourseFJSP(inst, w1=inp.w1, w2=inp.w2, w3=inp.w3, P=inp.P)
    algorithm = NSGA2(
        pop_size             = inp.pop_size,
        sampling             = FloatRandomSampling(),
        crossover            = SBX(prob=0.9, eta=15),
        mutation             = PM(prob=1.0 / problem.n_var, eta=20),
        eliminate_duplicates = True,
    )
    termination = get_termination("n_gen", inp.n_gen)

    t0  = time.perf_counter()
    res = pymoo_minimize(problem, algorithm, termination, seed=inp.seed, verbose=False)
    elapsed = round(time.perf_counter() - t0, 2)

    if res.F is None or res.X is None:
        return SolverResult(status="no_solution", solve_time=elapsed)

    # Many DIFFERENT decision vectors (different room/day/time assignments)
    # can land on the exact same objective values — NSGA2's
    # eliminate_duplicates only dedupes in decision space, not objective
    # space. Collapse those down to one representative per unique
    # (C_max, W_max, U_max, Idle) tuple so the results table/chart aren't
    # cluttered with dozens of visually-identical rows.
    F_rounded = np.round(res.F, 1)
    _, unique_idx = np.unique(F_rounded, axis=0, return_index=True)
    unique_idx = np.sort(unique_idx)   # preserve original front ordering
    F_unique = res.F[unique_idx]
    X_unique = res.X[unique_idx]

    # Decode a schedule for every unique solution FIRST, so we can check
    # feasibility (any unplaced session?) before deciding what to show.
    all_schedules_raw = [_build_schedule_entries(x, inst) for x in X_unique]
    n_unplaced_per_sol = [sum(1 for e in sched if e.classroom == -1) for sched in all_schedules_raw]

    feasible_mask = [n == 0 for n in n_unplaced_per_sol]
    n_feasible    = sum(feasible_mask)

    if n_feasible > 0:
        # Normal case: only show solutions where every session was actually
        # placed. A schedule with unplaced sessions isn't a usable timetable,
        # so it shouldn't be presented as a valid Pareto solution.
        keep_idx = [i for i, ok in enumerate(feasible_mask) if ok]
        status_msg = "ok"
    else:
        # No fully feasible solution was found at all (rare — usually means
        # pop_size/n_gen is too low, or the instance is over-constrained).
        # Fall back to showing the LEAST-bad solutions so the user isn't
        # left with an empty screen, but flag it clearly.
        keep_idx = list(range(len(X_unique)))
        status_msg = "ok_no_fully_feasible_solution"

    F_unique      = F_unique[keep_idx]
    X_unique      = X_unique[keep_idx]
    all_schedules = [all_schedules_raw[i] for i in keep_idx]
    n_filtered_out = len(all_schedules_raw) - len(all_schedules)

    pareto_solutions = [
        ParetoSolution(
            index = idx,
            C_max = round(float(f[0]), 1),
            W_max = round(float(f[1]), 1),
            U_max = round(float(f[2]), 1),
            Idle  = round(float(f[3]), 1),
        )
        for idx, f in enumerate(F_unique)
    ]

    weighted    = (inp.w1 * F_unique[:, 0] + inp.w2 * F_unique[:, 1]
                 + inp.w3 * F_unique[:, 2] + inp.P  * F_unique[:, 3])
    best_index  = int(np.argmin(weighted))
    best_schedule = all_schedules[best_index]

    return SolverResult(
        status            = status_msg,
        solve_time        = elapsed,
        pareto_solutions  = pareto_solutions,
        best_schedule     = best_schedule,
        all_schedules     = all_schedules,
        n_instances       = inst["n_instances"],
        n_theory_courses  = inst["n_theory_courses"],
        n_lab_courses     = inst["n_lab_courses"],
        n_classrooms      = inst["n_classrooms"],
        n_labs            = inst["n_labs"],
        n_faculty         = inst["n_faculty"],
        n_student_groups  = inst["n_student_groups"],
        groups            = list(inp.groups),
        courses           = [c.dict() for c in inp.courses],
        DAYS              = inst["DAYS"],
        Td                = inst["Td"],
    )