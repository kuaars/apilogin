from fastapi import FastAPI, Request, Form, Depends, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from starlette.middleware.sessions import SessionMiddleware

from fastapi.staticfiles import StaticFiles
from sqlalchemy import inspect, text
import uuid
from urllib.parse import urlparse
from pathlib import Path

from database import Base, SessionLocal, engine
from models import User, CartItem, Order, OrderItem, product as Product
from payments import FakePaymentProvider, get_provider

app = FastAPI()

app.add_middleware(
    SessionMiddleware,
    secret_key="change-this-to-a-long-random-secret"
)

templates = Jinja2Templates(directory="templates")

CURRENCY = "₽"
STATUS_LABELS = {
    "pending": "Ожидает оплаты",
    "paid": "Оплачен",
    "failed": "Оплата не прошла",
}


def money(value):
    return f"{int(value or 0):,}".replace(",", "\u00a0") + f"\u00a0{CURRENCY}"


templates.env.filters["money"] = money
templates.env.filters["status_label"] = lambda s: STATUS_LABELS.get(s, s)

# Платёжная система выбирается переменной окружения PAYMENT_PROVIDER (см. payments.py)
provider = get_provider()

Base.metadata.create_all(bind=engine)


def add_missing_columns():
    """create_all не меняет существующие таблицы, поэтому докидываем новые колонки."""
    insp = inspect(engine)
    with engine.begin() as conn:
        if "image" not in {c["name"] for c in insp.get_columns("products")}:
            conn.execute(text("ALTER TABLE products ADD COLUMN image VARCHAR"))
        if "admin" not in {c["name"] for c in insp.get_columns("users")}:
            conn.execute(text("ALTER TABLE users ADD COLUMN admin BOOLEAN DEFAULT 0"))


add_missing_columns()

UPLOAD_DIR = Path("uploads")
UPLOAD_DIR.mkdir(exist_ok=True)
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")

MAX_IMAGE_SIZE = 5 * 1024 * 1024  # 5 МБ

pwd_context = PasswordHasher()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def get_user(request: Request, db: Session):
    username = request.session.get("username")
    if not username:
        return None
    return db.query(User).filter(User.username == username).first()


def is_admin(user) -> bool:
    return bool(user and user.admin)


def admin_guard(request: Request, db: Session):
    """None, если доступ разрешён, иначе готовый ответ."""
    user = get_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    if not is_admin(user):
        return HTMLResponse("Доступ запрещён", status_code=403)
    return None


async def save_image(file: UploadFile):
    """Сохраняет картинку и возвращает имя файла. Без файла вернёт None."""
    if file is None or not file.filename:
        return None

    data = await file.read(MAX_IMAGE_SIZE + 1)
    if len(data) > MAX_IMAGE_SIZE:
        raise ValueError("Файл больше 5 МБ")

    # Тип определяем по содержимому, а не по имени файла
    if data.startswith(b"\xff\xd8\xff"):
        ext = ".jpg"
    elif data.startswith(b"\x89PNG\r\n\x1a\n"):
        ext = ".png"
    elif data.startswith(b"GIF8"):
        ext = ".gif"
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        ext = ".webp"
    else:
        raise ValueError("Допустимы только изображения JPG, PNG, GIF и WEBP")

    filename = f"{uuid.uuid4().hex}{ext}"
    (UPLOAD_DIR / filename).write_bytes(data)
    return filename


def render_admin(request: Request, db: Session, error=None, status_code=200):
    products = db.query(Product).order_by(Product.id.desc()).all()
    return templates.TemplateResponse(
        request=request,
        name="admin.html",
        context={
            "products": products,
            "error": error,
            "saved": request.query_params.get("saved"),
        },
        status_code=status_code
    )


def back_url(request: Request, default: str = "/") -> str:
    """Путь страницы, с которой пришёл запрос (только путь, без чужих доменов)."""
    ref = request.headers.get("referer")
    if ref:
        u = urlparse(ref)
        if u.path.startswith("/") and not u.path.startswith("//"):
            return u.path + (f"?{u.query}" if u.query else "")
    return default


def cart_count(db: Session, user) -> int:
    if not user:
        return 0
    items = db.query(CartItem).filter(CartItem.user_id == user.id).all()
    return sum(i.quantity for i in items)


def finalize_order(db: Session, order: Order, success: bool):
    """Единственное место, где заказ становится оплаченным. Повторный вызов ничего не меняет."""
    if order.status != "pending":
        return
    order.status = "paid" if success else "failed"
    db.commit()


def start_payment(request: Request, db: Session, order: Order):
    payment = provider.create_payment(order, return_url=str(request.base_url) + "orders")
    order.payment_id = payment["payment_id"]
    order.status = "pending"
    db.commit()
    return RedirectResponse(url=payment["redirect_url"], status_code=303)


def load_own_order(request: Request, db: Session, order_id: int):
    """Возвращает (заказ, None) или (None, готовый ответ)."""
    user = get_user(request, db)
    if not user:
        return None, RedirectResponse(url="/login", status_code=303)
    order = db.get(Order, order_id)
    if not order or order.user_id != user.id:
        raise HTTPException(status_code=404, detail="Заказ не найден")
    return order, None


@app.get('/', response_class=HTMLResponse)
async def main(
    request: Request,
    q: str = "",
    db: Session = Depends(get_db)
):
    q = q.strip()
    products = db.query(Product).order_by(Product.id.desc()).all()

    # Фильтруем в Python: SQLite LIKE не игнорирует регистр у кириллицы
    if q:
        needle = q.casefold()
        products = [
            p for p in products
            if needle in (p.name or "").casefold()
            or needle in (p.description or "").casefold()
        ]

    user = get_user(request, db)

    return templates.TemplateResponse(
        request=request,
        name="main.html",
        context={
            "products": products,
            "q": q,
            "is_admin": is_admin(user),
            "cart_count": cart_count(db, user)
        }
    )


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
async def profile(request: Request, db: Session = Depends(get_db)):

    username = request.session.get("username")

    if not username:
        return RedirectResponse(
            url="/login",
            status_code=303
        )

    return templates.TemplateResponse(
        request=request,
        name="profile.html",
        context={"username": username, "is_admin": is_admin(get_user(request, db))}
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
        username = username,
        password = hashed_password
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

@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request, db: Session = Depends(get_db)):
    denied = admin_guard(request, db)
    if denied:
        return denied
    return render_admin(request, db)


@app.post("/admin/products")
async def add_product(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    price: int = Form(...),
    image: UploadFile = File(None),
    db: Session = Depends(get_db)
):
    denied = admin_guard(request, db)
    if denied:
        return denied

    name = name.strip()
    if not name or price < 0:
        return render_admin(request, db, error="Укажите название и цену не меньше 0", status_code=400)

    try:
        filename = await save_image(image)
    except ValueError as e:
        return render_admin(request, db, error=str(e), status_code=400)

    db.add(Product(
        name=name,
        description=description.strip(),
        price=price,
        image=filename
    ))
    db.commit()

    return RedirectResponse(url="/admin?saved=1", status_code=303)


@app.post("/admin/products/{product_id}/image")
async def set_product_image(
    product_id: int,
    request: Request,
    image: UploadFile = File(None),
    db: Session = Depends(get_db)
):
    denied = admin_guard(request, db)
    if denied:
        return denied

    item = db.get(Product, product_id)
    if not item:
        raise HTTPException(status_code=404, detail="Товар не найден")

    try:
        filename = await save_image(image)
    except ValueError as e:
        return render_admin(request, db, error=str(e), status_code=400)

    if not filename:
        return render_admin(request, db, error="Выберите файл с фото", status_code=400)

    old = item.image
    item.image = filename
    db.commit()

    if old:
        (UPLOAD_DIR / Path(old).name).unlink(missing_ok=True)

    return RedirectResponse(url="/admin?saved=1", status_code=303)


# ---------------------------- Корзина ----------------------------

@app.post("/cart/add/{product_id}")
async def cart_add(product_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    if not db.get(Product, product_id):
        raise HTTPException(status_code=404, detail="Товар не найден")

    item = db.query(CartItem).filter(
        CartItem.user_id == user.id,
        CartItem.product_id == product_id
    ).first()

    if item:
        item.quantity = min((item.quantity or 0) + 1, 99)
    else:
        db.add(CartItem(user_id=user.id, product_id=product_id, quantity=1))
    db.commit()

    return RedirectResponse(url=back_url(request), status_code=303)


@app.get("/cart", response_class=HTMLResponse)
async def cart_page(request: Request, db: Session = Depends(get_db)):
    user = get_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    items = db.query(CartItem).filter(CartItem.user_id == user.id).order_by(CartItem.id).all()
    total = sum((i.product.price or 0) * i.quantity for i in items)

    return templates.TemplateResponse(
        request=request,
        name="cart.html",
        context={
            "items": items,
            "total": total,
            "cart_count": sum(i.quantity for i in items)
        }
    )


@app.post("/cart/item/{product_id}")
async def cart_item(
    product_id: int,
    request: Request,
    action: str = Form(...),
    db: Session = Depends(get_db)
):
    user = get_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    item = db.query(CartItem).filter(
        CartItem.user_id == user.id,
        CartItem.product_id == product_id
    ).first()

    if item:
        if action == "inc":
            item.quantity = min(item.quantity + 1, 99)
        elif action == "dec":
            item.quantity -= 1
        elif action == "remove":
            item.quantity = 0

        if item.quantity <= 0:
            db.delete(item)
        db.commit()

    return RedirectResponse(url="/cart", status_code=303)


# ---------------------------- Заказ и оплата ----------------------------

@app.post("/checkout")
async def checkout(request: Request, db: Session = Depends(get_db)):
    user = get_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    items = db.query(CartItem).filter(CartItem.user_id == user.id).all()
    if not items:
        return RedirectResponse(url="/cart", status_code=303)

    # Цены берём из базы, а не от клиента
    order = Order(user_id=user.id, status="pending", total=0)
    for i in items:
        price = i.product.price or 0
        order.items.append(OrderItem(
            product_id=i.product_id,
            name=i.product.name,
            price=price,
            quantity=i.quantity
        ))
        order.total += price * i.quantity
        db.delete(i)

    db.add(order)
    db.flush()  # получаем order.id

    return start_payment(request, db, order)


@app.get("/orders", response_class=HTMLResponse)
async def orders_page(request: Request, db: Session = Depends(get_db)):
    user = get_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    orders = db.query(Order).filter(Order.user_id == user.id).order_by(Order.id.desc()).all()

    return templates.TemplateResponse(
        request=request,
        name="orders.html",
        context={"orders": orders, "cart_count": cart_count(db, user)}
    )


@app.post("/orders/{order_id}/pay")
async def pay_order_again(order_id: int, request: Request, db: Session = Depends(get_db)):
    order, response = load_own_order(request, db, order_id)
    if response:
        return response
    if order.status == "paid":
        return RedirectResponse(url="/orders", status_code=303)
    return start_payment(request, db, order)


# Страницы фейковой оплаты. Работают только при PAYMENT_PROVIDER=fake.
@app.get("/pay/fake/{order_id}", response_class=HTMLResponse)
async def fake_pay_page(order_id: int, request: Request, db: Session = Depends(get_db)):
    if not isinstance(provider, FakePaymentProvider):
        raise HTTPException(status_code=404)

    order, response = load_own_order(request, db, order_id)
    if response:
        return response
    if order.status != "pending":
        return RedirectResponse(url="/orders", status_code=303)

    return templates.TemplateResponse(
        request=request,
        name="pay_fake.html",
        context={"order": order}
    )


@app.post("/pay/fake/{order_id}")
async def fake_pay_confirm(
    order_id: int,
    request: Request,
    result: str = Form(...),
    db: Session = Depends(get_db)
):
    if not isinstance(provider, FakePaymentProvider):
        raise HTTPException(status_code=404)

    order, response = load_own_order(request, db, order_id)
    if response:
        return response

    finalize_order(db, order, success=(result == "success"))
    return RedirectResponse(url="/orders", status_code=303)


# Сюда настоящая платёжка шлёт уведомления об оплате
@app.post("/payments/webhook")
async def payment_webhook(request: Request, db: Session = Depends(get_db)):
    try:
        data = provider.parse_webhook(await request.body(), dict(request.headers))
    except (ValueError, NotImplementedError):
        raise HTTPException(status_code=400, detail="Неверное уведомление")

    order = db.query(Order).filter(Order.payment_id == data["payment_id"]).first()
    if not order:
        raise HTTPException(status_code=404, detail="Заказ не найден")

    finalize_order(db, order, success=data["success"])
    return {"ok": True}


# ---------------------------- Заказы в админке ----------------------------

@app.get("/admin/orders", response_class=HTMLResponse)
async def admin_orders(request: Request, db: Session = Depends(get_db)):
    denied = admin_guard(request, db)
    if denied:
        return denied

    orders = db.query(Order).order_by(Order.id.desc()).all()

    return templates.TemplateResponse(
        request=request,
        name="admin_orders.html",
        context={"orders": orders}
    )
