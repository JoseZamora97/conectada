import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends, HTTPException, Request
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from sqlmodel import Session, select
from starlette.middleware.cors import CORSMiddleware

from app.auth import get_password_hash, get_user_by_username, verify_password, create_access_token, get_current_user
from app.config import settings
from app.database import create_db_and_tables, get_session
from app.models import UserRead, UserCreate, User, TaskCreate, Task, TaskRead, TaskBase

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("tasks_api")

limiter = Limiter(key_func=get_remote_address)


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Startup
    create_db_and_tables()
    logger.info("DB initialized")
    yield
    # Shutdown logic can go here if needed

app = FastAPI(
    title="Tasks API",
    version="1.0.0",
    lifespan=lifespan
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.post("/register", response_model=UserRead)
@limiter.limit("5/minute")
async def register(
    request: Request,
    user: UserCreate,
    session: Session = Depends(get_session)
):
    existing = session.exec(
        select(User).where(
            (User.username == user.username) |
            (User.email == user.email)
        )
    ).first()

    if existing:
        raise HTTPException(400, "User already exists")

    db_user = User(
        **user.model_dump(exclude={"password"}),
        hashed_password=get_password_hash(user.password)
    )

    session.add(db_user)
    session.commit()
    session.refresh(db_user)

    return db_user

@app.post("/login")
@limiter.limit("10/minute")
async def login(
        request: Request,
    username: str,
    password: str,
    session: Session = Depends(get_session)
):
    user = get_user_by_username(session, username)
    if not user or not verify_password(password, user.hashed_password):
        raise HTTPException(401, "Invalid credentials")

    token = create_access_token(data={"sub": user.username})
    return {"access_token": token, "token_type": "bearer"}


app.post("/tasks", response_model=TaskRead)
@limiter.limit(settings.rate_limit)
async def create_task(
    request: Request,
    task: TaskCreate,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session)
):
    db_task = Task(**task.model_dump(), owner_id=current_user.id)
    session.add(db_task)
    session.commit()
    session.refresh(db_task)
    return db_task


@app.get("/tasks", response_model=list[TaskRead])
@limiter.limit(settings.rate_limit)
async def get_tasks(
    request: Request,
    completed: bool | None = None,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session)
):
    statement = select(Task).where(Task.owner_id == current_user.id)
    if completed is not None:
        statement = statement.where(Task.completed == completed)
    return session.exec(statement).all()


@app.get("/tasks/{task_id}", response_model=TaskRead)
async def get_task(
    task_id: int,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session)
):
    task = session.get(Task, task_id)
    if not task or task.owner_id != current_user.id:
        raise HTTPException(404, "Not found")
    return task


@app.patch("/tasks/{task_id}", response_model=TaskRead)
async def update_task(
    task_id: int,
    task_update: TaskBase,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session)
):
    task = session.get(Task, task_id)
    if not task or task.owner_id != current_user.id:
        raise HTTPException(404)

    for key, value in task_update.model_dump(exclude_unset=True).items():
        setattr(task, key, value)

    session.add(task)
    session.commit()
    session.refresh(task)
    return task


@app.delete("/tasks/{task_id}", status_code=204)
async def delete_task(
    task_id: int,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session)
):
    task = session.get(Task, task_id)
    if not task or task.owner_id != current_user.id:
        raise HTTPException(404)
    session.delete(task)
    session.commit()
    return None

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)