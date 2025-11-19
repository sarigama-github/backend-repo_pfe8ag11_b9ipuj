import os
from typing import List, Optional, Any, Dict
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, EmailStr
from datetime import datetime, timezone
from bson import ObjectId

# Database helpers
from database import db, create_document, get_documents

app = FastAPI(title="Chat & Email API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --------- Utilities ---------
class PyObjectId(ObjectId):
    @classmethod
    def __get_validators__(cls):
        yield cls.validate

    @classmethod
    def validate(cls, v):
        if isinstance(v, ObjectId):
            return v
        if not ObjectId.is_valid(v):
            raise ValueError("Invalid ObjectId")
        return ObjectId(v)

def serialize_id(doc: Dict[str, Any]) -> Dict[str, Any]:
    if not doc:
        return doc
    d = {**doc}
    if "_id" in d:
        d["id"] = str(d.pop("_id"))
    # Convert any datetime to isoformat
    for k, v in list(d.items()):
        if isinstance(v, datetime):
            d[k] = v.isoformat()
        if isinstance(v, list):
            d[k] = [str(x) if isinstance(x, ObjectId) else x for x in v]
    return d

# --------- Models (Requests) ---------
class CreateUser(BaseModel):
    name: str
    email: EmailStr

class CreateConversation(BaseModel):
    participant_ids: List[str] = Field(..., min_items=2)
    title: Optional[str] = None

class SendMessage(BaseModel):
    conversation_id: str
    sender_id: str
    content: str

class SendEmail(BaseModel):
    sender: EmailStr
    to: List[EmailStr]
    subject: str
    body: str
    cc: Optional[List[EmailStr]] = []
    bcc: Optional[List[EmailStr]] = []

class UpdateEmailStatus(BaseModel):
    read: Optional[bool] = None
    folder: Optional[str] = None  # inbox, sent, drafts, trash, archived

# --------- Routes ---------
@app.get("/")
def root():
    return {"message": "Chat & Email API running"}

@app.get("/test")
def test_database():
    response = {
        "backend": "✅ Running",
        "database": "❌ Not Available",
        "database_url": "✅ Set" if os.getenv("DATABASE_URL") else "❌ Not Set",
        "database_name": "✅ Set" if os.getenv("DATABASE_NAME") else "❌ Not Set",
        "connection_status": "Not Connected",
        "collections": []
    }
    try:
        if db is not None:
            response["database"] = "✅ Available"
            response["connection_status"] = "Connected"
            try:
                response["collections"] = db.list_collection_names()
                response["database"] = "✅ Connected & Working"
            except Exception as e:
                response["database"] = f"⚠️ Connected but Error: {str(e)[:80]}"
        else:
            response["database"] = "⚠️ Available but not initialized"
    except Exception as e:
        response["database"] = f"❌ Error: {str(e)[:80]}"
    return response

# Users
@app.post("/api/users")
def create_user(payload: CreateUser):
    existing = db["user"].find_one({"email": payload.email}) if db else None
    if existing:
        raise HTTPException(status_code=400, detail="Email already exists")
    user_id = create_document("user", {"name": payload.name, "email": payload.email})
    doc = db["user"].find_one({"_id": ObjectId(user_id)})
    return serialize_id(doc)

@app.get("/api/users")
def list_users():
    docs = get_documents("user") if db else []
    return [serialize_id(d) for d in docs]

# Conversations & Messages
@app.post("/api/conversations")
def create_conversation(payload: CreateConversation):
    # Validate participants
    participant_oids = []
    for pid in payload.participant_ids:
        if not ObjectId.is_valid(pid):
            raise HTTPException(status_code=400, detail="Invalid participant id")
        participant_oids.append(ObjectId(pid))
    conv = {
        "participants": participant_oids,
        "title": payload.title or "Conversation",
        "last_message": None,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }
    res = db["conversation"].insert_one(conv)
    doc = db["conversation"].find_one({"_id": res.inserted_id})
    return serialize_id(doc)

@app.get("/api/conversations")
def list_conversations(user_id: Optional[str] = Query(None)):
    filt = {}
    if user_id and ObjectId.is_valid(user_id):
        filt = {"participants": ObjectId(user_id)}
    docs = list(db["conversation"].find(filt).sort("updated_at", -1))
    return [serialize_id(d) for d in docs]

@app.get("/api/conversations/{conversation_id}")
def get_conversation(conversation_id: str):
    if not ObjectId.is_valid(conversation_id):
        raise HTTPException(status_code=400, detail="Invalid id")
    doc = db["conversation"].find_one({"_id": ObjectId(conversation_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Not found")
    return serialize_id(doc)

@app.get("/api/conversations/{conversation_id}/messages")
def list_messages(conversation_id: str):
    if not ObjectId.is_valid(conversation_id):
        raise HTTPException(status_code=400, detail="Invalid id")
    msgs = list(db["message"].find({"conversation_id": ObjectId(conversation_id)}).sort("created_at", 1))
    return [serialize_id(m) for m in msgs]

@app.post("/api/messages")
def send_message(payload: SendMessage):
    if not ObjectId.is_valid(payload.conversation_id) or not ObjectId.is_valid(payload.sender_id):
        raise HTTPException(status_code=400, detail="Invalid ids")
    message = {
        "conversation_id": ObjectId(payload.conversation_id),
        "sender_id": ObjectId(payload.sender_id),
        "content": payload.content,
        "created_at": datetime.now(timezone.utc),
    }
    res = db["message"].insert_one(message)
    # Update conversation last_message and updated_at
    db["conversation"].update_one(
        {"_id": ObjectId(payload.conversation_id)},
        {"$set": {"last_message": payload.content, "updated_at": datetime.now(timezone.utc)}}
    )
    doc = db["message"].find_one({"_id": res.inserted_id})
    return serialize_id(doc)

# Email endpoints
@app.post("/api/emails")
def create_email(payload: SendEmail):
    email_doc = {
        "sender": str(payload.sender),
        "to": [str(x) for x in payload.to],
        "cc": [str(x) for x in (payload.cc or [])],
        "bcc": [str(x) for x in (payload.bcc or [])],
        "subject": payload.subject,
        "body": payload.body,
        "read": False,
        "folder": "sent",  # sender's perspective
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }
    res = db["email"].insert_one(email_doc)
    # Also create copies for recipients in their inbox folder
    inbox_docs = []
    now = datetime.now(timezone.utc)
    for recipient in email_doc["to"]:
        inbox_docs.append({**email_doc, "_id": None, "folder": "inbox", "owner": recipient, "created_at": now, "updated_at": now})
    if inbox_docs:
        # remove _id None handling
        for d in inbox_docs:
            d.pop("_id", None)
        db["email"].insert_many(inbox_docs)
    return serialize_id(db["email"].find_one({"_id": res.inserted_id}))

@app.get("/api/emails")
def list_emails(owner: Optional[str] = Query(None), folder: Optional[str] = Query(None)):
    filt: Dict[str, Any] = {}
    if owner:
        filt["owner"] = owner
    if folder:
        filt["folder"] = folder
    docs = list(db["email"].find(filt).sort("created_at", -1))
    return [serialize_id(d) for d in docs]

@app.patch("/api/emails/{email_id}")
def update_email(email_id: str, payload: UpdateEmailStatus):
    if not ObjectId.is_valid(email_id):
        raise HTTPException(status_code=400, detail="Invalid id")
    updates: Dict[str, Any] = {"updated_at": datetime.now(timezone.utc)}
    if payload.read is not None:
        updates["read"] = payload.read
    if payload.folder is not None:
        updates["folder"] = payload.folder
    res = db["email"].update_one({"_id": ObjectId(email_id)}, {"$set": updates})
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Not found")
    doc = db["email"].find_one({"_id": ObjectId(email_id)})
    return serialize_id(doc)

# Optional: simple search
@app.get("/api/search")
def search(q: str = Query("")):
    if not q:
        return {"conversations": [], "emails": []}
    convs = list(db["conversation"].find({"title": {"$regex": q, "$options": "i"}}).limit(10))
    emails = list(db["email"].find({"$or": [{"subject": {"$regex": q, "$options": "i"}}, {"body": {"$regex": q, "$options": "i"}}]}).limit(10))
    return {
        "conversations": [serialize_id(c) for c in convs],
        "emails": [serialize_id(e) for e in emails]
    }

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
