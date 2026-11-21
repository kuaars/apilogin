from fastapi import FastAPI, Request, Form, Depends, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from starlette.middleware.sessions import SessionMiddleware

from fastapi.staticfiles import StaticFiles
from sqlalchemy import inspect, text
import json
import uuid
from urllib.parse import urlparse
from pathlib import Path

from database import Base, SessionLocal, engine
from models import User, CartItem, Order, OrderItem, Category, product as Product
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
    "processing": "Собирается",
    "shipped": "Отправлен",
    "delivered": "Доставлен",
    "cancelled": "Отменён",
    "failed": "Оплата не прошла",
}

# Статусы, при которых заказ считается оплаченным (для статистики в профиле)
PAID_STATUSES = {"paid", "processing", "shipped", "delivered"}

# Контакты поддержки для профиля: поменяйте на свои
SUPPORT = {
    "email": "support@shop.example",
    "telegram": "shop_support",
    "hours": "ежедневно с 9:00 до 21:00",
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
        if "category_id" not in {c["name"] for c in insp.get_columns("products")}:
            conn.execute(text("ALTER TABLE products ADD COLUMN category_id INTEGER"))
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
    categories = db.query(Category).order_by(Category.id).all()
    options = list(flatten_tree(build_tree(categories)))

    import_result = None
    imported = request.query_params.get("imported")
    if imported:
        try:
            created, added, updated = (int(x) for x in imported.split(","))
            import_result = {"categories": created, "products": added, "updated": updated}
        except ValueError:
            pass

    return templates.TemplateResponse(
        request=request,
        name="admin.html",
        context={
            "products": products,
            "error": error,
            "saved": request.query_params.get("saved"),
            "category_options": options,
            "category_names": {c["id"]: c["name"] for c in options},
            "import_result": import_result,
        },
        status_code=status_code
    )


# ---------------------------- Категории ----------------------------

def build_tree(categories):
    """Плоский список категорий -> дерево словарей {id, name, children}."""
    nodes = {c.id: {"id": c.id, "name": c.name, "children": []} for c in categories}
    roots = []
    for c in categories:
        if c.parent_id in nodes:
            nodes[c.parent_id]["children"].append(nodes[c.id])
        else:
            roots.append(nodes[c.id])
    return roots


def flatten_tree(nodes, depth=0):
    for n in nodes:
        yield {"id": n["id"], "name": n["name"], "depth": depth}
        yield from flatten_tree(n["children"], depth + 1)


def category_family(categories, cat_id):
    """id категории и всех её подкатегорий (любой глубины)."""
    ids = {cat_id}
    changed = True
    while changed:
        changed = False
        for c in categories:
            if c.parent_id in ids and c.id not in ids:
                ids.add(c.id)
                changed = True
    return ids


def category_ancestors(categories, cat_id):
    by_id = {c.id: c for c in categories}
    ids = set()
    cur = by_id.get(cat_id)
    while cur and cur.parent_id in by_id and cur.parent_id not in ids:
        ids.add(cur.parent_id)
        cur = by_id[cur.parent_id]
    return ids


def parse_category_id(db: Session, value: str):
    """'' -> None (без категории). Несуществующая категория -> ValueError."""
    if not value or not value.isdigit():
        return None
    if not db.get(Category, int(value)):
        raise ValueError("Категория не найдена")
    return int(value)


# ---------------------------- Импорт каталога из JSON ----------------------------

MAX_IMPORT_SIZE = 2 * 1024 * 1024  # 2 МБ
MAX_CATEGORY_DEPTH = 6


def import_products(db: Session, items, category_id, path, stats):
    if not isinstance(items, list):
        raise ValueError(f"{path}: ожидается список")

    for i, item in enumerate(items):
        where = f"{path}[{i}]"
        if not isinstance(item, dict):
            raise ValueError(f"{where}: ожидается объект")

        name = item.get("name")
        price = item.get("price")
        description = item.get("description", "")

        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"{where}: у товара нужно поле name")
        if isinstance(price, bool) or not isinstance(price, int) or price < 0:
            raise ValueError(f"{where}: price должна быть целым числом не меньше 0")
        if not isinstance(description, str):
            raise ValueError(f"{where}: description должно быть строкой")

        name = name.strip()
        existing = db.query(Product).filter(
            Product.name == name,
            Product.category_id == category_id
        ).first()

        if existing:
            existing.price = price
            existing.description = description.strip()
            stats["updated"] += 1
        else:
            db.add(Product(
                name=name,
                description=description.strip(),
                price=price,
                category_id=category_id
            ))
            stats["products"] += 1
        db.flush()


def import_category(db: Session, node, parent_id, path, stats, depth):
    if depth > MAX_CATEGORY_DEPTH:
        raise ValueError(f"{path}: слишком глубокая вложенность (максимум {MAX_CATEGORY_DEPTH} уровней)")
    if not isinstance(node, dict):
        raise ValueError(f"{path}: ожидается объект")

    name = node.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"{path}: у категории нужно поле name")
    name = name.strip()

    category = db.query(Category).filter(
        Category.name == name,
        Category.parent_id == parent_id
    ).first()

    if not category:
        category = Category(name=name, parent_id=parent_id)
        db.add(category)
        db.flush()
        stats["categories"] += 1

    import_products(db, node.get("products", []), category.id, f"{path}.products", stats)

    subcategories = node.get("subcategories", [])
    if not isinstance(subcategories, list):
        raise ValueError(f"{path}.subcategories: ожидается список")
    for i, sub_node in enumerate(subcategories):
        import_category(db, sub_node, category.id, f"{path}.subcategories[{i}]", stats, depth + 1)


def import_catalog(db: Session, data):
    """Добавляет категории и товары из JSON. Существующие совпадения обновляет, дубликатов не создаёт."""
    if not isinstance(data, dict):
        raise ValueError("Корень файла должен быть объектом { ... }")

    stats = {"categories": 0, "products": 0, "updated": 0}

    categories = data.get("categories", [])
    if not isinstance(categories, list):
        raise ValueError("categories: ожидается список")
    for i, node in enumerate(categories):
        import_category(db, node, None, f"categories[{i}]", stats, 1)

    import_products(db, data.get("products", []), None, "products", stats)
    return stats


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
    cat: str = "",
    db: Session = Depends(get_db)
):
    q = q.strip()
    cat_id = int(cat) if cat.isdigit() else None
    categories = db.query(Category).order_by(Category.id).all()
    products = db.query(Product).order_by(Product.id.desc()).all()

    active_cat_name = None
    open_ids = set()
    if cat_id is not None:
        current = next((c for c in categories if c.id == cat_id), None)
        if current:
            active_cat_name = current.name
            # раскрываем в меню путь до выбранной категории
            open_ids = category_ancestors(categories, cat_id) | {cat_id}
            family = category_family(categories, cat_id)
            products = [p for p in products if p.category_id in family]
        else:
            cat_id = None

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
            "cart_count": cart_count(db, user),
            "category_tree": build_tree(categories),
            "active_cat": cat_id,
            "active_cat_name": active_cat_name,
            "open_ids": open_ids
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

    user = get_user(request, db)

    if not user:
        return RedirectResponse(
            url="/login",
            status_code=303
        )

    orders = db.query(Order).filter(Order.user_id == user.id).order_by(Order.id.desc()).all()
    paid = [o for o in orders if o.status in PAID_STATUSES]
    spent = sum(o.total or 0 for o in paid)

    stats = {
        "orders": len(orders),
        "spent": spent,
        "average": spent // len(paid) if paid else 0,
        "bought": sum(i.quantity or 0 for o in paid for i in o.items),
    }

    # Сводка по магазину видна только админу
    shop = None
    if is_admin(user):
        all_orders = db.query(Order).all()
        shop = {
            "revenue": sum(o.total or 0 for o in all_orders if o.status in PAID_STATUSES),
            "waiting": sum(1 for o in all_orders if o.status == "pending"),
            "in_work": sum(1 for o in all_orders if o.status in ("paid", "processing")),
        }

    return templates.TemplateResponse(
        request=request,
        name="profile.html",
        context={
            "username": user.username,
            "is_admin": is_admin(user),
            "stats": stats,
            "recent_orders": orders[:3],
            "shop": shop,
            "support": SUPPORT,
            "cart_count": cart_count(db, user)
        }
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
    category_id: str = Form(""),
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
        cat_id = parse_category_id(db, category_id)
    except ValueError as e:
        return render_admin(request, db, error=str(e), status_code=400)

    try:
        filename = await save_image(image)
    except ValueError as e:
        return render_admin(request, db, error=str(e), status_code=400)

    db.add(Product(
        name=name,
        description=description.strip(),
        price=price,
        image=filename,
        category_id=cat_id
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
    if order.status not in ("pending", "failed"):
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
        context={"orders": orders, "statuses": STATUS_LABELS}
    )


# ---------------------------- Категории и импорт в админке ----------------------------

@app.post("/admin/categories")
async def add_category(
    request: Request,
    name: str = Form(...),
    parent_id: str = Form(""),
    db: Session = Depends(get_db)
):
    denied = admin_guard(request, db)
    if denied:
        return denied

    name = name.strip()
    if not name:
        return render_admin(request, db, error="Укажите название категории", status_code=400)

    try:
        parent = parse_category_id(db, parent_id)
    except ValueError as e:
        return render_admin(request, db, error=str(e), status_code=400)

    if db.query(Category).filter(Category.name == name, Category.parent_id == parent).first():
        return render_admin(request, db, error="Такая категория уже есть на этом уровне", status_code=400)

    db.add(Category(name=name, parent_id=parent))
    db.commit()

    return RedirectResponse(url="/admin?saved=1", status_code=303)


@app.post("/admin/products/{product_id}/category")
async def set_product_category(
    product_id: int,
    request: Request,
    category_id: str = Form(""),
    db: Session = Depends(get_db)
):
    denied = admin_guard(request, db)
    if denied:
        return denied

    item = db.get(Product, product_id)
    if not item:
        raise HTTPException(status_code=404, detail="Товар не найден")

    try:
        item.category_id = parse_category_id(db, category_id)
    except ValueError as e:
        return render_admin(request, db, error=str(e), status_code=400)

    db.commit()
    return RedirectResponse(url="/admin?saved=1", status_code=303)


@app.post("/admin/import")
async def admin_import(
    request: Request,
    file: UploadFile = File(None),
    db: Session = Depends(get_db)
):
    denied = admin_guard(request, db)
    if denied:
        return denied

    if file is None or not file.filename:
        return render_admin(request, db, error="Выберите JSON-файл", status_code=400)

    raw = await file.read(MAX_IMPORT_SIZE + 1)
    if len(raw) > MAX_IMPORT_SIZE:
        return render_admin(request, db, error="Файл больше 2 МБ", status_code=400)

    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        return render_admin(request, db, error=f"Не удалось прочитать JSON: {e}", status_code=400)

    # Всё или ничего: при ошибке в любом месте файла в базу не попадёт ничего
    try:
        stats = import_catalog(db, data)
        db.commit()
    except ValueError as e:
        db.rollback()
        return render_admin(request, db, error=str(e), status_code=400)

    result = f"{stats['categories']},{stats['products']},{stats['updated']}"
    return RedirectResponse(url=f"/admin?imported={result}", status_code=303)


@app.post("/admin/orders/{order_id}/status")
async def admin_set_order_status(
    order_id: int,
    request: Request,
    status: str = Form(...),
    db: Session = Depends(get_db)
):
    denied = admin_guard(request, db)
    if denied:
        return denied

    order = db.get(Order, order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Заказ не найден")
    if status not in STATUS_LABELS:
        raise HTTPException(status_code=400, detail="Неизвестный статус")

    order.status = status
    db.commit()

    return RedirectResponse(url="/admin/orders?saved=1", status_code=303)