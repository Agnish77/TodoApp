import os
import redis
import json
import threading
import time
import hashlib
import logging
from datetime import datetime, timedelta

from flask import Flask, render_template, request, redirect, jsonify, flash, g
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

from prometheus_flask_exporter import PrometheusMetrics

# ============================================================================
# APP INITIALIZATION
# ============================================================================

app = Flask(__name__)

# ============================================================================
# PROMETHEUS METRICS
# ============================================================================

metrics = PrometheusMetrics(app, group_by="endpoint")

REQUEST_COUNT = metrics.counter(
    "http_requests_total",
    "Total HTTP requests",
    labels={
        "method": lambda: request.method,
        "endpoint": lambda: request.endpoint or "unknown",
    },
)

# ============================================================================
# PERFORMANCE MIDDLEWARE
# ============================================================================

@app.before_request
def before_request():
    g.start_time = time.time()
    g.request_id = hashlib.md5(
        f"{time.time()}{request.remote_addr}".encode()
    ).hexdigest()[:16]


@app.after_request
def after_request(response):

    if hasattr(g, "start_time"):

        elapsed = time.time() - g.start_time

        response.headers["X-Request-ID"] = getattr(g, "request_id", "unknown")
        response.headers["X-Response-Time-ms"] = int(elapsed * 1000)

        if elapsed > 0.5:
            app.logger.warning(
                json.dumps(
                    {
                        "event": "slow_request",
                        "request_id": g.request_id,
                        "path": request.path,
                        "method": request.method,
                        "duration_ms": elapsed * 1000,
                        "user": current_user.id
                        if current_user.is_authenticated
                        else "anonymous",
                    }
                )
            )

    return response


# ============================================================================
# HEALTH CHECK
# ============================================================================

@app.route("/health")
def health():

    health = {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "service": "todo-saas",
    }

    try:
        db.session.execute("SELECT 1")
        health["database"] = "connected"
    except Exception as e:
        health["database"] = str(e)
        health["status"] = "degraded"

    try:
        redis_client.ping()
        health["redis"] = "connected"
    except Exception as e:
        health["redis"] = str(e)
        health["status"] = "degraded"

    status = 200 if health["status"] == "healthy" else 503

    return jsonify(health), status


@app.route("/ready")
def ready():
    return jsonify({"status": "ready"})


# ============================================================================
# CONFIG
# ============================================================================

app.secret_key = os.getenv("SECRET_KEY", "dev-secret")

app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv(
    "DATABASE_URL", "sqlite:///todo.db"
)

app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

swagger = Swagger(app)

# ============================================================================
# JWT
# ============================================================================

app.config["JWT_SECRET_KEY"] = os.getenv("JWT_SECRET_KEY", "jwt-secret")
app.config["JWT_ACCESS_TOKEN_EXPIRES"] = timedelta(minutes=30)

# ============================================================================
# REDIS
# ============================================================================

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")

try:

    redis_client = redis.from_url(REDIS_URL)
    redis_client.ping()

    print("Redis connected")

except Exception:

    class DummyRedis:
        def __init__(self):
            self.data = {}

        def get(self, k):
            return self.data.get(k)

        def setex(self, k, t, v):
            self.data[k] = v

        def delete(self, k):
            self.data.pop(k, None)

        def ping(self):
            return True

        def publish(self, *args):
            pass

    redis_client = DummyRedis()

# ============================================================================
# QUEUE
# ============================================================================

try:
    task_queue = Queue(connection=redis_client)
except:
    task_queue = None

# ============================================================================
# EXTENSIONS
# ============================================================================

db.init_app(app)

bcrypt = Bcrypt(app)

login_manager = LoginManager(app)

login_manager.login_view = "login"

jwt = JWTManager(app)

limiter = Limiter(get_remote_address, app=app)

socketio = SocketIO(app, cors_allowed_origins="*")

# ============================================================================
# LOGIN LOADER
# ============================================================================

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


# ============================================================================
# CACHE
# ============================================================================

def get_cached_todos(user_id):

    key = f"todos:{user_id}"

    cached = redis_client.get(key)

    if cached:
        return json.loads(cached)

    todos = Todo.query.filter_by(user_id=user_id).all()

    data = [
        {
            "id": t.id,
            "title": t.title,
            "desc": t.desc,
            "completed": t.completed,
        }
        for t in todos
    ]

    redis_client.setex(key, 60, json.dumps(data))

    return data


def invalidate_cache(user_id):
    redis_client.delete(f"todos:{user_id}")


# ============================================================================
# API LOGIN
# ============================================================================

@app.route("/api/login", methods=["POST"])
@limiter.limit("5/minute")
def api_login():

    data = request.get_json()

    user = User.query.filter_by(username=data.get("username")).first()

    if not user or not bcrypt.check_password_hash(
        user.password, data.get("password")
    ):
        return jsonify({"error": "Invalid credentials"}), 401

    token = create_access_token(identity=user.id)

    return jsonify({"access_token": token})


# ============================================================================
# GET TODOS API
# ============================================================================

@app.route("/api/todos")
@jwt_required()
def get_todos():

    user_id = get_jwt_identity()

    todos = Todo.query.filter_by(user_id=user_id).all()

    return jsonify(
        [
            {
                "id": t.id,
                "title": t.title,
                "desc": t.desc,
                "completed": t.completed,
            }
            for t in todos
        ]
    )


# ============================================================================
# CREATE TODO API
# ============================================================================

@app.route("/api/todos", methods=["POST"])
@jwt_required()
def create_todo():

    user_id = get_jwt_identity()

    data = request.get_json()

    todo = Todo(title=data.get("title"), desc=data.get("desc"), user_id=user_id)

    db.session.add(todo)

    db.session.commit()

    invalidate_cache(user_id)

    return jsonify({"message": "todo created"})


# ============================================================================
# WEB ROUTES
# ============================================================================

@app.route("/", methods=["GET", "POST"])
@login_required
def index():

    if request.method == "POST":

        todo = Todo(
            title=request.form["title"],
            desc=request.form["desc"],
            user_id=current_user.id,
        )

        db.session.add(todo)

        db.session.commit()

        invalidate_cache(current_user.id)

        return redirect("/")

    todos = Todo.query.filter_by(user_id=current_user.id).all()

    return render_template("index.html", todos=todos)


@app.route("/signup", methods=["GET", "POST"])
def signup():

    if request.method == "POST":

        username = request.form["username"]

        if User.query.filter_by(username=username).first():
            flash("username exists")
            return redirect("/signup")

        password = bcrypt.generate_password_hash(
            request.form["password"]
        ).decode("utf-8")

        user = User(username=username, password=password)

        db.session.add(user)

        db.session.commit()

        return redirect("/login")

    return render_template("signup.html")


@app.route("/login", methods=["GET", "POST"])
def login():

    if request.method == "POST":

        user = User.query.filter_by(username=request.form["username"]).first()

        if not user:
            flash("user not found")
            return redirect("/login")

        if not bcrypt.check_password_hash(
            user.password, request.form["password"]
        ):
            flash("wrong password")
            return redirect("/login")

        login_user(user)

        return redirect("/")

    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect("/login")


# ============================================================================
# DB INIT
# ============================================================================

with app.app_context():
    db.create_all()

# ============================================================================
# START SERVER
# ============================================================================

if __name__ == "__main__":

    print("Todo SaaS Running")

    print("http://localhost:5000")

    print("metrics → /metrics")

    socketio.run(app, host="0.0.0.0", port=5000, debug=True)
