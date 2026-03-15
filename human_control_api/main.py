from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional
import redis
import json
import time

app = FastAPI(title="Human Control API", version="1.0.0")

r = redis.Redis(host="redis", port=6379, decode_responses=True)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_state() -> dict:
    state = r.get("system:state")
    if state:
        return json.loads(state)
    return {
        "orchestrator_state": "VAST_HOST_JOB",
        "mining_enabled": True,
        "gpu_5090_locked_vast": False,
        "gaming_pc_extended": False,
        "gaming_pc_offline": False,
        "agents_halted": False,
        "updated_at": time.time(),
    }

def save_state(state: dict):
    state["updated_at"] = time.time()
    r.set("system:state", json.dumps(state))

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class RejectPayload(BaseModel):
    comments: str

# ---------------------------------------------------------------------------
# System state endpoints
# ---------------------------------------------------------------------------

@app.post("/control/maintenance")
def enter_maintenance():
    state = get_state()
    state["orchestrator_state"] = "MAINTENANCE"
    save_state(state)
    r.publish("orchestrator:events", json.dumps({"event": "MAINTENANCE"}))
    return {"status": "ok", "orchestrator_state": "MAINTENANCE"}

@app.post("/control/resume")
def exit_maintenance():
    state = get_state()
    if state["orchestrator_state"] != "MAINTENANCE":
        raise HTTPException(status_code=400, detail="System is not in MAINTENANCE state")
    state["orchestrator_state"] = "VAST_HOST_JOB"
    save_state(state)
    r.publish("orchestrator:events", json.dumps({"event": "RESUME"}))
    return {"status": "ok", "orchestrator_state": "VAST_HOST_JOB"}

# ---------------------------------------------------------------------------
# Mining endpoints
# ---------------------------------------------------------------------------

@app.post("/control/mining/enable")
def enable_mining():
    state = get_state()
    state["mining_enabled"] = True
    save_state(state)
    r.publish("mining:control", json.dumps({"command": "enable"}))
    return {"status": "ok", "mining_enabled": True}

@app.post("/control/mining/disable")
def disable_mining():
    state = get_state()
    state["mining_enabled"] = False
    save_state(state)
    r.publish("mining:control", json.dumps({"command": "disable"}))
    return {"status": "ok", "mining_enabled": False}

# ---------------------------------------------------------------------------
# Gaming PC endpoints
# ---------------------------------------------------------------------------

@app.post("/control/gaming-pc/extend")
def extend_gaming_pc():
    state = get_state()
    state["gaming_pc_extended"] = True
    state["gaming_pc_offline"] = False
    save_state(state)
    r.publish("orchestrator:events", json.dumps({"event": "GAMING_PC_EXTENDED"}))
    return {"status": "ok", "gaming_pc_extended": True}

@app.post("/control/gaming-pc/offline")
def gaming_pc_offline():
    state = get_state()
    state["gaming_pc_offline"] = True
    state["gaming_pc_extended"] = False
    save_state(state)
    r.publish("orchestrator:events", json.dumps({"event": "GAMING_PC_OFFLINE"}))
    return {"status": "ok", "gaming_pc_offline": True}

# ---------------------------------------------------------------------------
# GPU endpoints
# ---------------------------------------------------------------------------

@app.post("/control/gpu/5090/lock-vast")
def lock_5090_vast():
    state = get_state()
    state["gpu_5090_locked_vast"] = True
    save_state(state)
    r.publish("gpu:control", json.dumps({"command": "lock_vast", "gpu": "5090"}))
    return {"status": "ok", "gpu_5090_locked_vast": True}

@app.post("/control/gpu/5090/release")
def release_5090():
    state = get_state()
    state["gpu_5090_locked_vast"] = False
    save_state(state)
    r.publish("gpu:control", json.dumps({"command": "release", "gpu": "5090"}))
    return {"status": "ok", "gpu_5090_locked_vast": False}

# ---------------------------------------------------------------------------
# Agent endpoints
# ---------------------------------------------------------------------------

@app.post("/agents/halt-all")
def halt_all_agents():
    state = get_state()
    state["agents_halted"] = True
    save_state(state)
    r.publish("agents:control", json.dumps({"command": "halt_all"}))
    return {"status": "ok", "agents_halted": True}

# ---------------------------------------------------------------------------
# Task endpoints
# ---------------------------------------------------------------------------

@app.post("/tasks/{task_id}/approve")
def approve_task(task_id: str):
    task_key = f"task:{task_id}"
    task = r.get(task_key)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    task_data = json.loads(task)
    task_data["status"] = "approved"
    task_data["approved_at"] = time.time()
    r.set(task_key, json.dumps(task_data))
    r.lrem("approval_queue", 0, task_id)
    r.publish("tasks:approvals", json.dumps({"task_id": task_id, "decision": "approved"}))
    return {"status": "ok", "task_id": task_id, "decision": "approved"}

@app.post("/tasks/{task_id}/reject")
def reject_task(task_id: str, payload: RejectPayload):
    task_key = f"task:{task_id}"
    task = r.get(task_key)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    task_data = json.loads(task)
    task_data["status"] = "rejected"
    task_data["rejected_at"] = time.time()
    task_data["rejection_comments"] = payload.comments
    r.set(task_key, json.dumps(task_data))
    r.lrem("approval_queue", 0, task_id)
    r.publish("tasks:approvals", json.dumps({
        "task_id": task_id,
        "decision": "rejected",
        "comments": payload.comments
    }))
    return {"status": "ok", "task_id": task_id, "decision": "rejected"}

# ---------------------------------------------------------------------------
# Read endpoints
# ---------------------------------------------------------------------------

@app.get("/status")
def get_status():
    state = get_state()
    queue_depths = {
        "pm_inbox": r.llen("pm_inbox"),
        "dev_queue": r.llen("dev_queue"),
        "review_queue": r.llen("review_queue"),
        "test_queue": r.llen("test_queue"),
        "media_queue": r.llen("media_queue"),
        "gpu_jobs": r.llen("gpu_jobs"),
        "approval_queue": r.llen("approval_queue"),
    }
    return {
        "system": state,
        "queue_depths": queue_depths,
    }

@app.get("/tasks/pending")
def get_pending_tasks():
    task_ids = r.lrange("approval_queue", 0, -1)
    tasks = []
    for task_id in task_ids:
        task = r.get(f"task:{task_id}")
        if task:
            tasks.append(json.loads(task))
    return {"pending": tasks, "count": len(tasks)}

@app.get("/health")
def health():
    try:
        r.ping()
        return {"status": "ok", "redis": "connected"}
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Redis unavailable: {e}")
