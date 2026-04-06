import os
import redis
import json
import threading
from datetime import timedelta

from flask import Flask, render_template, request, redirect, jsonify, flash
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from flask_bcrypt import Bcrypt
from flask_jwt_extended import JWTManager, create_access_token, jwt_required, get_jwt_identity
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_socketio import SocketIO
from flasgger import Swagger

from rq import Queue

from data import db
from model import User, Todo


# =====================================================
# APP CONFIG
# =====================================================

app = Flask(__name__)

app.secret_key = os.getenv("SECRET_KEY", "dev-secret")

app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv(
    "DATABASE_URL",
    "sqlite:///todo.db"
)

app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

swagger = Swagger(app)


# =====================================================
# JWT CONFIG
# =====================================================

app.config["JWT_SECRET_KEY"] = os.getenv("JWT_SECRET_KEY", "jwt-secret")
app.config["JWT_ACCESS_TOKEN_EXPIRES"] = timedelta(minutes=30)


# =====================================================
# REDIS CONFIG
# =====================================================

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")

redis_client = redis.from_url(REDIS_URL)

task_queue = Queue(connection=redis_client)


# =====================================================
# EXTENSIONS
# =====================================================

db.init_app(app)

bcrypt = Bcrypt(app)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"

jwt = JWTManager(app)

limiter = Limiter(
    get_remote_address,
    app=app,
    storage_uri=REDIS_URL,
    default_limits=["200 per day", "50 per hour"]
)

socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    message_queue=REDIS_URL,
    async_mode="eventlet"
)


# =====================================================
# LOGIN MANAGER
# =====================================================

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


# =====================================================
# REDIS EVENT SYSTEM
# =====================================================

def publish_event(event):
    redis_client.publish("todo_events", event)


def redis_listener():

    pubsub = redis_client.pubsub()
    pubsub.subscribe("todo_events")

    for message in pubsub.listen():

        if message["type"] == "message":
            socketio.emit("todo_update")


def start_event_listener():

    thread = threading.Thread(target=redis_listener)
    thread.daemon = True
    thread.start()


# =====================================================
# CACHE SYSTEM
# =====================================================

def get_cached_todos(user_id):

    cache_key = f"todos:{user_id}"

    cached = redis_client.get(cache_key)

    if cached:
        return json.loads(cached)

    todos = Todo.query.filter_by(user_id=user_id).all()

    result = [
        {
            "id": t.id,
            "title": t.title,
            "desc": t.desc,
            "completed": t.completed
        }
        for t in todos
    ]

    redis_client.setex(cache_key, 60, json.dumps(result))

    return result


def invalidate_cache(user_id):

    redis_client.delete(f"todos:{user_id}")


# =====================================================
# BACKGROUND JOB
# =====================================================

def log_event(event):

    print("Background job:", event)


# =====================================================
# API LOGIN
# =====================================================

@app.route("/api/login", methods=["POST"])
@limiter.limit("5 per minute")
def api_login():
    """
    User Login
    ---
    tags:
      - Authentication
    consumes:
      - application/json
    parameters:
      - in: body
        name: body
        schema:
          type: object
          properties:
            username:
              type: string
              example: user1
            password:
              type: string
              example: password123
    responses:
      200:
        description: JWT access token
      401:
        description: Invalid credentials
    """

    data = request.get_json()

    user = User.query.filter_by(
        username=data.get("username")
    ).first()

    if not user or not bcrypt.check_password_hash(
        user.password,
        data.get("password")
    ):
        return jsonify({"error": "Invalid credentials"}), 401

    token = create_access_token(identity=user.id)

    return jsonify({"access_token": token})


# =====================================================
# GET TODOS API
# =====================================================

@app.route("/api/todos", methods=["GET"])
@jwt_required()
def get_todos():
    """
    Get Todos
    ---
    tags:
      - Todos
    parameters:
      - name: page
        in: query
        type: integer
        default: 1
      - name: limit
        in: query
        type: integer
        default: 10
    responses:
      200:
        description: List of todos
    """

    user_id = get_jwt_identity()

    page = int(request.args.get("page", 1))
    limit = int(request.args.get("limit", 10))

    query = Todo.query.filter_by(user_id=user_id)

    todos = query.paginate(page=page, per_page=limit)

    return jsonify({
        "page": page,
        "limit": limit,
        "total": todos.total,
        "data": [
            {
                "id": t.id,
                "title": t.title,
                "desc": t.desc,
                "completed": t.completed
            }
            for t in todos.items
        ]
    })


# =====================================================
# CREATE TODO API
# =====================================================

@app.route("/api/todos", methods=["POST"])
@jwt_required()
def create_todo_api():
    """
    Create Todo
    ---
    tags:
      - Todos
    consumes:
      - application/json
    parameters:
      - in: body
        name: body
        schema:
          type: object
          properties:
            title:
              type: string
              example: Buy milk
            desc:
              type: string
              example: from supermarket
    responses:
      200:
        description: Todo created
    """

    user_id = get_jwt_identity()

    data = request.get_json()

    todo = Todo(
        title=data.get("title"),
        desc=data.get("desc"),
        user_id=user_id
    )

    db.session.add(todo)
    db.session.commit()

    invalidate_cache(user_id)

    publish_event("todo_created")

    task_queue.enqueue(log_event, "todo created")

    return jsonify({"message": "Todo created"})


# =====================================================
# SIGNUP
# =====================================================

@app.route("/signup", methods=["GET", "POST"])
def signup():

    if request.method == "POST":

        username = request.form["username"]

        if User.query.filter_by(username=username).first():
            flash("Username exists")
            return redirect("/signup")

        password = bcrypt.generate_password_hash(
            request.form["password"]
        ).decode("utf-8")

        user = User(username=username, password=password)

        db.session.add(user)
        db.session.commit()

        return redirect("/login")

    return render_template("signup.html")


# =====================================================
# LOGIN
# =====================================================

@app.route("/login", methods=["GET", "POST"])
def login():

    if request.method == "POST":

        user = User.query.filter_by(
            username=request.form["username"]
        ).first()

        if not user:
            flash("User not found")
            return redirect("/login")

        if not bcrypt.check_password_hash(
            user.password,
            request.form["password"]
        ):
            flash("Wrong password")
            return redirect("/login")

        login_user(user)

        return redirect("/")

    return render_template("login.html")


# =====================================================
# LOGOUT
# =====================================================

@app.route("/logout")
@login_required
def logout():

    logout_user()

    return redirect("/login")


# =====================================================
# DASHBOARD
# =====================================================

@app.route("/", methods=["GET", "POST"])
@login_required
def index():

    if request.method == "POST":

        todo = Todo(
            title=request.form["title"],
            desc=request.form["desc"],
            user_id=current_user.id
        )

        db.session.add(todo)
        db.session.commit()

        invalidate_cache(current_user.id)

        publish_event("todo_created")

        task_queue.enqueue(log_event, "todo created")

        return redirect("/")

    search = request.args.get("search")

    query = Todo.query.filter_by(user_id=current_user.id)

    if search:
        query = query.filter(Todo.title.contains(search))

    todos = query.order_by(Todo.date_c.desc()).all()

    return render_template("index.html", todos=todos)


# =====================================================
# DELETE
# =====================================================

@app.route("/delete/<int:id>")
@login_required
def delete(id):

    todo = Todo.query.filter_by(
        id=id,
        user_id=current_user.id
    ).first_or_404()

    db.session.delete(todo)
    db.session.commit()

    invalidate_cache(current_user.id)

    publish_event("todo_deleted")

    return redirect("/")


# =====================================================
# TOGGLE
# =====================================================

@app.route("/toggle/<int:id>")
@login_required
def toggle(id):

    todo = Todo.query.filter_by(
        id=id,
        user_id=current_user.id
    ).first_or_404()

    todo.completed = not todo.completed

    db.session.commit()

    invalidate_cache(current_user.id)

    publish_event("todo_updated")

    return redirect("/")


# =====================================================
# API DOCS PAGE
# =====================================================

@app.route("/api-docs")
def api_docs():
    return render_template("api_docs.html")


# =====================================================
# START SERVER
# =====================================================

if __name__ == "__main__":

    start_event_listener()

    socketio.run(app)
