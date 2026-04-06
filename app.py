import os
import redis
import json
import threading
import time
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

# ============================================================================
# PROMETHEUS METRICS (FAANG-READY - FIXED VERSION)
# ============================================================================
from prometheus_flask_exporter import PrometheusMetrics

app = Flask(__name__)

# Initialize Prometheus metrics correctly
metrics = PrometheusMetrics(app)

# Create custom metrics properly
@app.before_request
def before_request():
    g.start_time = time.time()

@app.after_request
def after_request(response):
    if hasattr(g, 'start_time'):
        elapsed = time.time() - g.start_time
        # Add response time header
        response.headers['X-Response-Time'] = f"{elapsed*1000:.2f}ms"
        
        # Log slow requests
        if elapsed > 0.5:
            app.logger.warning(f'Slow request: {request.path} took {elapsed:.3f}s')
    return response

# ============================================================================
# APP CONFIG
# ============================================================================

app.secret_key = os.getenv("SECRET_KEY", "dev-secret")

app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv(
    "DATABASE_URL",
    "sqlite:///todo.db"
)

app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

swagger = Swagger(app)

# ============================================================================
# JWT CONFIG
# ============================================================================

app.config["JWT_SECRET_KEY"] = os.getenv("JWT_SECRET_KEY", "jwt-secret")
app.config["JWT_ACCESS_TOKEN_EXPIRES"] = timedelta(minutes=30)

# ============================================================================
# REDIS CONFIG
# ============================================================================

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")

try:
    redis_client = redis.from_url(REDIS_URL)
    redis_client.ping()
    print("✅ Redis connected")
except:
    class DummyRedis:
        def __init__(self):
            self._data = {}
        def get(self, key): return self._data.get(key)
        def setex(self, key, time, value): self._data[key] = value
        def delete(self, key): 
            if key in self._data: del self._data[key]
        def ping(self): return True
        def publish(self, channel, message): return 0
    redis_client = DummyRedis()

task_queue = Queue(connection=redis_client) if not isinstance(redis_client, DummyRedis) else None

# ============================================================================
# EXTENSIONS
# ============================================================================

db.init_app(app)
bcrypt = Bcrypt(app)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"

jwt = JWTManager(app)

limiter = Limiter(get_remote_address, app=app, default_limits=["200 per day", "50 per hour"])

socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

# ============================================================================
# LOGIN MANAGER
# ============================================================================

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# ============================================================================
# REDIS EVENT SYSTEM
# ============================================================================

def publish_event(event):
    try:
        if not isinstance(redis_client, DummyRedis):
            redis_client.publish("todo_events", event)
    except:
        pass

def redis_listener():
    try:
        if not isinstance(redis_client, DummyRedis):
            pubsub = redis_client.pubsub()
            pubsub.subscribe("todo_events")
            for message in pubsub.listen():
                if message["type"] == "message":
                    socketio.emit("todo_update")
    except:
        pass

def start_event_listener():
    if not isinstance(redis_client, DummyRedis):
        thread = threading.Thread(target=redis_listener)
        thread.daemon = True
        thread.start()

# ============================================================================
# CACHE SYSTEM
# ============================================================================

def get_cached_todos(user_id):
    cache_key = f"todos:{user_id}"
    try:
        cached = redis_client.get(cache_key)
        if cached:
            return json.loads(cached)
    except:
        pass
    
    todos = Todo.query.filter_by(user_id=user_id).all()
    result = [{"id": t.id, "title": t.title, "desc": t.desc, "completed": t.completed} for t in todos]
    
    try:
        redis_client.setex(cache_key, 60, json.dumps(result))
    except:
        pass
    return result

def invalidate_cache(user_id):
    try:
        redis_client.delete(f"todos:{user_id}")
    except:
        pass

def log_event(event):
    print("Background job:", event)

# ============================================================================
# HEALTH CHECK (KUBERNETES READY)
# ============================================================================

@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "service": "todo-saas"
    }), 200

@app.route('/metrics', methods=['GET'])
def metrics_endpoint():
    return "Prometheus metrics available at /metrics"

# ============================================================================
# API ROUTES
# ============================================================================

@app.route("/api/login", methods=["POST"])
@limiter.limit("5 per minute")
def api_login():
    data = request.get_json()
    user = User.query.filter_by(username=data.get("username")).first()
    if not user or not bcrypt.check_password_hash(user.password, data.get("password")):
        return jsonify({"error": "Invalid credentials"}), 401
    token = create_access_token(identity=user.id)
    return jsonify({"access_token": token})

@app.route("/api/todos", methods=["GET"])
@jwt_required()
def get_todos():
    user_id = get_jwt_identity()
    page = int(request.args.get("page", 1))
    limit = int(request.args.get("limit", 10))
    query = Todo.query.filter_by(user_id=user_id)
    todos = query.paginate(page=page, per_page=limit)
    return jsonify({
        "page": page,
        "limit": limit,
        "total": todos.total,
        "data": [{"id": t.id, "title": t.title, "desc": t.desc, "completed": t.completed} for t in todos.items]
    })

@app.route("/api/todos", methods=["POST"])
@jwt_required()
def create_todo_api():
    user_id = get_jwt_identity()
    data = request.get_json()
    todo = Todo(title=data.get("title"), desc=data.get("desc"), user_id=user_id)
    db.session.add(todo)
    db.session.commit()
    invalidate_cache(user_id)
    publish_event("todo_created")
    if task_queue:
        task_queue.enqueue(log_event, "todo created")
    return jsonify({"message": "Todo created"})

# ============================================================================
# WEB ROUTES
# ============================================================================

@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        username = request.form["username"]
        if User.query.filter_by(username=username).first():
            flash("Username exists")
            return redirect("/signup")
        password = bcrypt.generate_password_hash(request.form["password"]).decode("utf-8")
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
            flash("User not found")
            return redirect("/login")
        if not bcrypt.check_password_hash(user.password, request.form["password"]):
            flash("Wrong password")
            return redirect("/login")
        login_user(user)
        return redirect("/")
    return render_template("login.html")

@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect("/login")

@app.route("/", methods=["GET", "POST"])
@login_required
def index():
    if request.method == "POST":
        todo = Todo(title=request.form["title"], desc=request.form["desc"], user_id=current_user.id)
        db.session.add(todo)
        db.session.commit()
        invalidate_cache(current_user.id)
        publish_event("todo_created")
        if task_queue:
            task_queue.enqueue(log_event, "todo created")
        return redirect("/")
    
    search = request.args.get("search")
    query = Todo.query.filter_by(user_id=current_user.id)
    if search:
        query = query.filter(Todo.title.contains(search))
    todos = query.order_by(Todo.date_c.desc()).all()
    return render_template("index.html", todos=todos)

@app.route("/update/<int:id>", methods=["GET", "POST"])
@login_required
def update(id):
    todo = Todo.query.filter_by(id=id, user_id=current_user.id).first_or_404()
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        desc = request.form.get("desc", "").strip()
        if not title:
            flash("Title is required!", "danger")
            return redirect(f"/update/{id}")
        todo.title = title
        todo.desc = desc
        try:
            db.session.commit()
            invalidate_cache(current_user.id)
            publish_event("todo_updated")
            flash("Task updated successfully!", "success")
            return redirect("/")
        except:
            db.session.rollback()
            flash("Error updating task", "danger")
    return render_template("update.html", todo=todo)

@app.route("/delete/<int:id>")
@login_required
def delete(id):
    todo = Todo.query.filter_by(id=id, user_id=current_user.id).first_or_404()
    db.session.delete(todo)
    db.session.commit()
    invalidate_cache(current_user.id)
    publish_event("todo_deleted")
    return redirect("/")

@app.route("/toggle/<int:id>")
@login_required
def toggle(id):
    todo = Todo.query.filter_by(id=id, user_id=current_user.id).first_or_404()
    todo.completed = not todo.completed
    db.session.commit()
    invalidate_cache(current_user.id)
    publish_event("todo_updated")
    return redirect("/")

@app.route("/api-docs")
def api_docs():
    return render_template("api_docs.html")

# ============================================================================
# DATABASE SETUP
# ============================================================================

with app.app_context():
    db.create_all()
    print("✅ Database ready")

# ============================================================================
# START SERVER
# ============================================================================

if __name__ == "__main__":
    start_event_listener()
    print("\n" + "="*50)
    print("🚀 Todo SaaS with Prometheus")
    print("📊 Metrics: http://localhost:5000/metrics")
    print("="*50 + "\n")
    socketio.run(app, host="0.0.0.0", port=5000, debug=True)
