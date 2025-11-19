import os
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Literal, Optional, Dict, Any
from datetime import datetime, timezone

from database import db, create_document, get_documents
from schemas import Session as SessionSchema, Message as MessageSchema, Preview as PreviewSchema

try:
    from bson import ObjectId  # type: ignore
except Exception:
    ObjectId = str  # fallback typing

app = FastAPI(title="CoPilot API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def read_root():
    return {"message": "CoPilot Backend is running"}


@app.get("/api/hello")
def hello():
    return {"message": "Hello from the backend API!"}


@app.get("/test")
def test_database():
    response = {
        "backend": "✅ Running",
        "database": "❌ Not Available",
        "database_url": None,
        "database_name": None,
        "connection_status": "Not Connected",
        "collections": []
    }

    try:
        if db is not None:
            response["database"] = "✅ Available"
            response["database_url"] = "✅ Set" if os.getenv("DATABASE_URL") else "❌ Not Set"
            response["database_name"] = getattr(db, 'name', None) or ("✅ Set" if os.getenv("DATABASE_NAME") else "❌ Not Set")
            response["connection_status"] = "Connected"
            try:
                collections = db.list_collection_names()
                response["collections"] = collections[:10]
                response["database"] = "✅ Connected & Working"
            except Exception as e:
                response["database"] = f"⚠️  Connected but Error: {str(e)[:50]}"
        else:
            response["database"] = "⚠️  Available but not initialized"
    except Exception as e:
        response["database"] = f"❌ Error: {str(e)[:50]}"

    response["database_url"] = "✅ Set" if os.getenv("DATABASE_URL") else "❌ Not Set"
    response["database_name"] = "✅ Set" if os.getenv("DATABASE_NAME") else "❌ Not Set"

    return response


# ----- CoPilot Core API -----

class CreateSessionRequest(BaseModel):
    user_id: Optional[str] = None
    mode: Literal["resume", "interview", "jobs"]
    title: Optional[str] = None

class CreateSessionResponse(BaseModel):
    session_id: str

class ChatRequest(BaseModel):
    # single user message
    content: str

class ChatResponse(BaseModel):
    session_id: str
    messages: List[Dict[str, Any]]
    preview: Optional[Dict[str, Any]] = None


def _coerce_oid(value: Any) -> str:
    try:
        return str(value)
    except Exception:
        return value


def _generate_assistant_reply(mode: str, prompt: str) -> Dict[str, Any]:
    # Simple deterministic assistant and preview generator
    if mode == "resume":
        reply = {
            "text": "I've drafted a professional summary and key bullet points based on your input. You can ask me to refine tone, quantify impact, or tailor to specific roles.",
            "preview": {
                "type": "resume",
                "summary": f"{prompt.strip().capitalize()} professional with a track record of delivering measurable outcomes.",
                "sections": [
                    {"title": "Highlights", "items": [
                        f"Led initiatives related to {prompt[:60]}...",
                        "Improved efficiency by 20% through process optimization",
                        "Collaborated across teams to deliver on-time projects"
                    ]},
                ]
            }
        }
        return reply
    elif mode == "interview":
        topics = [w for w in prompt.split() if len(w) > 3][:3]
        questions = [
            f"Tell me about a time you worked with {topics[0] if topics else 'a cross-functional team'}.",
            f"How would you approach {topics[1] if len(topics) > 1 else 'prioritizing conflicting deadlines'}?",
            f"Describe a challenging {topics[2] if len(topics) > 2 else 'project'} and your impact."
        ]
        reply = {
            "text": "Here are tailored practice questions and guidance. Ask me to generate follow-ups or score your answers.",
            "preview": {
                "type": "interview",
                "questions": questions,
                "tips": [
                    "Use STAR format (Situation, Task, Action, Result)",
                    "Quantify impact and highlight collaboration",
                    "Tie answers back to role requirements"
                ]
            }
        }
        return reply
    else:  # jobs
        keyword = prompt.split()[0] if prompt.strip() else "Role"
        jobs = [
            {"title": f"{keyword.capitalize()} Specialist", "company": "Acme Corp", "location": "Remote", "match": 92},
            {"title": f"Senior {keyword.capitalize()}", "company": "Nimbus", "location": "NYC, NY", "match": 88},
            {"title": f"{keyword.capitalize()} Analyst", "company": "Orbit Labs", "location": "Austin, TX", "match": 83}
        ]
        reply = {
            "text": "I found a few matching roles. Ask me to tailor your resume or draft outreach messages.",
            "preview": {
                "type": "jobs",
                "results": jobs
            }
        }
        return reply


@app.post("/api/sessions", response_model=CreateSessionResponse)
def create_session(payload: CreateSessionRequest):
    session = SessionSchema(**{
        "user_id": payload.user_id,
        "mode": payload.mode,
        "title": payload.title,
        "status": "active"
    })
    session_id = create_document("session", session)

    # Seed with a system message
    create_document("message", MessageSchema(
        session_id=session_id,
        role="system",
        content=f"CoPilot session created for {payload.mode} mode."
    ))
    return {"session_id": session_id}


@app.get("/api/sessions/{session_id}/messages")
def list_messages(session_id: str):
    docs = get_documents("message", {"session_id": session_id})
    for d in docs:
        d["_id"] = _coerce_oid(d.get("_id"))
        d["created_at"] = d.get("created_at")
        d["updated_at"] = d.get("updated_at")
    return {"items": docs}


@app.post("/api/sessions/{session_id}/messages", response_model=ChatResponse)
def chat(session_id: str, payload: ChatRequest):
    # verify session exists
    sess = db["session"].find_one({"_id": ObjectId(session_id)}) if hasattr(ObjectId, "__call__") else db["session"].find_one({"_id": session_id})
    if not sess:
        raise HTTPException(status_code=404, detail="Session not found")

    mode = sess.get("mode", "resume")

    # store user message
    create_document("message", MessageSchema(
        session_id=session_id,
        role="user",
        content=payload.content
    ))

    # generate assistant reply and preview
    result = _generate_assistant_reply(mode, payload.content)

    create_document("message", MessageSchema(
        session_id=session_id,
        role="assistant",
        content=result["text"],
        meta={"mode": mode}
    ))

    if result.get("preview"):
        preview_doc = PreviewSchema(session_id=session_id, mode=mode, content=result["preview"]).model_dump()
        preview_doc["created_at"] = datetime.now(timezone.utc)
        preview_doc["updated_at"] = datetime.now(timezone.utc)
        db["preview"].insert_one(preview_doc)

    msgs = get_documents("message", {"session_id": session_id})
    for m in msgs:
        m["_id"] = _coerce_oid(m.get("_id"))

    return {
        "session_id": session_id,
        "messages": msgs,
        "preview": result.get("preview")
    }


@app.get("/api/sessions/{session_id}/preview")
def get_preview(session_id: str):
    doc = db["preview"].find_one({"session_id": session_id}, sort=[["created_at", -1]])
    if not doc:
        return {"session_id": session_id, "preview": None}
    doc["_id"] = _coerce_oid(doc.get("_id"))
    return {"session_id": session_id, "preview": doc.get("content")}


# Optional schema explorer for Flames tooling
@app.get("/schema")
def get_schema_definitions():
    return {
        "collections": [
            "session",
            "message",
            "preview",
            "user",
            "product",
        ]
    }


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
