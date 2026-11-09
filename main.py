from fastapi import FastAPI, Request, Form, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from starlette.middleware.sessions import SessionMiddleware

from database import Base, SessionLocal, engine
from models import User

app = FastAPI()

app.add_middleware(
    SessionMiddleware,
    secret_key="change-this-to-a-long-random-secret"
)

templates = Jinja2Templates(directory="templates")

Base.metadata.create_all(bind=engine)

pwd_context = PasswordHasher()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={}
    )


@app.post("/login")
async def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db)
):
    user = db.query(User).filter(
        User.username == username
    ).first()

    if not user:
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"error": "Неверный логин или пароль"},
            status_code=401
        )

    try:
        pwd_context.verify(user.password, password)
    except VerifyMismatchError:
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"error": "Неверный логин или пароль"},
            status_code=401
        )

    request.session["username"] = user.username

    return RedirectResponse(
        url="/profile",
        status_code=303
    )


@app.get("/profile", response_class=HTMLResponse)
async def profile(request: Request):

    username = request.session.get("username")

    if not username:
        return RedirectResponse(
            url="/login",
            status_code=303
        )

    return templates.TemplateResponse(
        request=request,
        name="profile.html",
        context={"username": username}
    )

@app.get("/register")
async def register_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="reg.html",
        context={}
    )

@app.post("/register")
async def register(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db)
):
    username = username.strip()


    if len(username) < 3 or len(username) > 30:
        return templates.TemplateResponse(
            request=request,
            name="reg.html",
            context={"error": "Логин должен содержать от 3 до 30 символов"},
            status_code=400
        )

    if len(password) < 8:
        return templates.TemplateResponse(
            request=request,
            name="reg.html",
            context={"error": "Пароль должен содержать минимум 8 символов"},
            status_code=400
        )

    user = db.query(User).filter(
        User.username == username
    ).first()

    if user:
        return templates.TemplateResponse(
            request=request,
            name="reg.html",
            context={"error": "Пользователь уже существует"},
            status_code=400
        )

    hashed_password = pwd_context.hash(password)

    new_user = User(
        username=username,
        password=hashed_password
    )

    db.add(new_user)
    db.commit()
    db.refresh(new_user)

    return RedirectResponse(
        url="/login",
        status_code=303
    )

@app.get("/users")
async def get_users(db: Session = Depends(get_db)):
    users = db.query(User).all()

    return [
        {
            "id": user.id,
            "username": user.username
        }
        for user in users
    ]

@app.get("/")
async def start(request: Request):
    return RedirectResponse(
        url="/register",
        status_code=303
    )


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()

    return RedirectResponse(
        url="/login",
        status_code=303
    )