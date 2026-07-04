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
from fastapi.responses import FileResponse
from models import SchedulerInput, SolverResult

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