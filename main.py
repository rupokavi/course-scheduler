"""
main.py
-------
FastAPI server for the University Course Scheduler.
Run with: uvicorn main:app --reload
"""

import uuid
from typing import Dict
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse
from models import SchedulerInput, SolverResult
from excel_export import build_routine_workbook, workbook_to_bytes

app = FastAPI(title="University Course Scheduler", version="2.0")

jobs: Dict[str, dict] = {}


def run_solver(job_id: str, inp: SchedulerInput):
    try:
        jobs[job_id]["status"] = "running"
        from solver import solve
        result: SolverResult = solve(inp)
        jobs[job_id]["status"] = "done"
        jobs[job_id]["result"] = result.dict()
    except Exception as e:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"]  = str(e)


app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def home():
    return FileResponse("static/index.html")

@app.get("/results-page")
def results_page():
    return FileResponse("static/results.html")

@app.get("/routine-page")
def routine_page():
    return FileResponse("static/routine.html")

@app.get("/analysis-page")
def analysis_page():
    return FileResponse("static/analysis.html")


@app.post("/solve")
def start_solver(inp: SchedulerInput, background_tasks: BackgroundTasks):
    job_id = str(uuid.uuid4())
    jobs[job_id] = {"status": "queued", "result": None, "error": None}
    background_tasks.add_task(run_solver, job_id, inp)
    return {"job_id": job_id, "status": "queued"}


@app.get("/status/{job_id}")
def get_status(job_id: str):
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    job = jobs[job_id]
    return {"job_id": job_id, "status": job["status"], "error": job.get("error")}


@app.get("/results/{job_id}")
def get_results(job_id: str):
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    job = jobs[job_id]
    if job["status"] != "done":
        raise HTTPException(status_code=400, detail=f"Job status: {job['status']}")
    return job["result"]


@app.get("/routine/{job_id}/{solution_index}")
def get_routine(job_id: str, solution_index: int):
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    job = jobs[job_id]
    if job["status"] != "done":
        raise HTTPException(status_code=400, detail="Job not done yet")

    result = job["result"]
    all_schedules = result.get("all_schedules") or []

    if 0 <= solution_index < len(all_schedules):
        schedule = all_schedules[solution_index]
    else:
        # Fallback for older result payloads without all_schedules
        schedule = result["best_schedule"]

    return {
        "solution_index": solution_index,
        "schedule": schedule,
        "pareto":   result["pareto_solutions"][solution_index]
                    if solution_index < len(result["pareto_solutions"])
                    else result["pareto_solutions"][0],
        "meta": {
            "DAYS":             result["DAYS"],
            "Td":               result["Td"],
            "n_classrooms":     result["n_classrooms"],
            "n_labs":           result.get("n_labs", 0),
            "n_faculty":        result["n_faculty"],
            "n_student_groups": result["n_student_groups"],
            "n_theory_courses": result["n_theory_courses"],
            "n_lab_courses":    result["n_lab_courses"],
            "groups":           result.get("groups", []),
            "courses":          result.get("courses", []),
        }
    }


@app.get("/routine/{job_id}/{solution_index}/excel")
def download_routine_excel(job_id: str, solution_index: int):
    """Same routine data as /routine/{job_id}/{solution_index}, rendered as
    an .xlsx that visually matches the official RUET IPE routine template."""
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    job = jobs[job_id]
    if job["status"] != "done":
        raise HTTPException(status_code=400, detail="Job not done yet")

    result = job["result"]
    all_schedules = result.get("all_schedules") or []
    if 0 <= solution_index < len(all_schedules):
        schedule = all_schedules[solution_index]
    else:
        schedule = result["best_schedule"]

    meta = {
        "DAYS":             result["DAYS"],
        "n_student_groups": result["n_student_groups"],
        "groups":           result.get("groups", []),
    }

    wb = build_routine_workbook(schedule, meta)
    buf = workbook_to_bytes(wb)
    filename = f"IPE_Class_Routine_sol{solution_index + 1}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )