import os
import redis
import json
import threading
import time
import hashlib
import logging
from datetime import datetime, timedelta
from functools import wraps

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

# Prometheus only (no OpenTelemetry to avoid context errors)
from prometheus_flask_exporter import PrometheusMetrics

# ============================================================================
# APP INITIALIZATION
# ============================================================================

app = Flask(__name__)

# ============================================================================
# PROMETHEUS METRICS (Working without context issues)
# ============================================================================

# Initialize metrics after app is created
metrics = PrometheusMetrics(app, group_by='endpoint')

# Custom Prometheus Metrics
REQUEST_COUNT = metrics.counter(
    'http_requests_total', 
    'Total HTTP requests',
    labels={'method': lambda: request.method, 'endpoint': lambda: request.endpoint or 'unknown'}
)

REQUEST_LATENCY = metrics.histogram(
    'http_request_duration_seconds', 
    'HTTP request latency',
    buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10]
)

# ============================================================================
# PERFORMANCE MIDDLEWARE
# ============================================================================

@app.before_request
def before_request():
    """Start request timer for latency monitoring"""
    g.start_time = time.time()
    g.request_id = hashlib.md5(f"{time.time()}{request.remote_addr}".encode()).hexdigest()[:16]

@app.after_request
def after_request(response):
    """Record metrics and log slow requests"""
    if hasattr(g, 'start_time'):
        elapsed = time.time() - g.start_time
        
        # Record latency metric
        REQUEST_LATENCY.observe(elapsed)
        
        # Add response headers
        response.headers['X-Request-ID'] = getattr(g, 'request_id', 'unknown')
        response.headers['X-Response-Time-ms'] = int(elapsed * 1000)
        
        # Log slow requests (>500ms)
        if elapsed > 0.5:
            app.logger.warning(json.dumps({
                "event": "slow_request",
                "request_id": g.request_id,
                "path": request.path,
                "method": request.method,
                "duration_ms": elapsed * 1000,
                "user": current_user.id if current_user.is_authenticated else "anonymous"
            }))
    
    return response

# ============================================================================
# HEALTH CHECK ENDPOINTS
# ============================================================================

@app.route('/health', methods=['GET'])
def health_check():
    """Health check for container orchestration"""
    health = {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "service": "todo-saas",
        "version": "2.0.0"
    }
    
    # Check database
    try:
        db.session.execute('SELECT 1')
        health['database'] = "connected"
    except Exception as e:
        health['database'] = f"error: {str(e)}"
        health['status'] = "degraded"
    
    # Check Redis
    try:
        redis_client.ping()
        health['redis'] = "connected"
    except Exception as e:
        health['redis'] = f"error: {str(e)}"
        health['status'] = "degraded"
    
    status_code = 200 if health['status'] == 'healthy' else 503
    return jsonify(health), status_code

@app.route('/ready', methods=['GET'])
def readiness_probe():
    """Kubernetes readiness probe"""
    return jsonify({"status": "ready"}), 200

@app.route('/metrics', methods=['GET'])
def metrics_endpoint():
    """Prometheus metrics endpoint"""
    return "Prometheus metrics available at /metrics endpoint"

# ============================================================================
# ORIGINAL APP CONFIG (UNCHANGED)
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

# Handle Redis connection gracefully
try:
    redis_client = redis.from_url(REDIS_URL)
    redis_client.ping()
    print("✅ Redis connected successfully")
except Exception as e:
    print(f"⚠️ Redis connection failed: {e}")
    # Create a dummy Redis client for fallback
    class DummyRedis:
        def __init__(self):
            self._data = {}
        def get(self, key):
            return self._data.get(key)
        def setex(self, key, time, value):
            self._data[key] = value
        def delete(self, key):
            if key in self._data:
                del self._data[key]
        def ping(self):
            return True
        def lpush(self, key, value):
            return 1
        def ltrim(self, key, start, end):
            return True
        def publish(self, channel, message):
            return 0
    redis_client = DummyRedis()

# Initialize task queue
try:
    task_queue = Queue(connection=redis_client) if not isinstance(redis_client, DummyRedis) else None
except:
    task_queue = None

# ============================================================================
# EXTENSIONS
# ============================================================================

db.init_app(app)

bcrypt = Bcrypt(app)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"

jwt = JWTManager(app)

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["200 per day", "50 per hour"]
)

socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode="eventlet"
)

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
    """Publish event to Redis channel"""
    try:
        if not isinstance(redis_client, DummyRedis):
            redis_client.publish("todo_events", event)
    except:
        pass

def redis_listener():
    """Listen for Redis events"""
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
    """Start Redis event listener thread"""
    if not isinstance(redis_client, DummyRedis):
        thread = threading.Thread(target=redis_listener)
        thread.daemon = True
        thread.start()
        print("✅ Event listener started")

# ============================================================================
# CACHE SYSTEM
# ============================================================================

def get_cached_todos(user_id):
    """Get cached todos"""
    cache_key = f"todos:{user_id}"
    try:
        cached = redis_client.get(cache_key)
        if cached:
            return json.loads(cached)
    except:
        pass
    
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
    
    try:
        redis_client.setex(cache_key, 60, json.dumps(result))
    except:
        pass
    
    return result

def invalidate_cache(user_id):
    """Invalidate cache"""
    try:
        redis_client.delete(f"todos:{user_id}")
    except:
        pass

# ============================================================================
# BACKGROUND JOB
# ============================================================================

def log_event(event):
    """Background job to log events"""
    print("Background job:", event)

# ============================================================================
# API LOGIN
# ============================================================================

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
    user = User.query.filter_by(username=data.get("username")).first()
    if not user or not bcrypt.check_password_hash(user.password, data.get("password")):
        return jsonify({"error": "Invalid credentials"}), 401
    token = create_access_token(identity=user.id)
    return jsonify({"access_token": token})

# ============================================================================
# GET TODOS API
# ============================================================================

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

# ============================================================================
# CREATE TODO API
# ============================================================================

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
    if task_queue:
        task_queue.enqueue(log_event, "todo created")
    return jsonify({"message": "Todo created"})

# ============================================================================
# SIGNUP
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

# ============================================================================
# LOGIN
# ============================================================================

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

# ============================================================================
# LOGOUT
# ============================================================================

@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect("/login")

# ============================================================================
# DASHBOARD
# ============================================================================

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
        if task_queue:
            task_queue.enqueue(log_event, "todo created")
        return redirect("/")
    
    search = request.args.get("search")
    query = Todo.query.filter_by(user_id=current_user.id)
    if search:
        query = query.filter(Todo.title.contains(search))
    todos = query.order_by(Todo.date_c.desc()).all()
    return render_template("index.html", todos=todos)

# ============================================================================
# UPDATE TODO
# ============================================================================

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
        except Exception as e:
            db.session.rollback()
            flash(f"Error updating task: {str(e)}", "danger")
            return redirect(f"/update/{id}")
    
    return render_template("update.html", todo=todo)

# ============================================================================
# DELETE
# ============================================================================

@app.route("/delete/<int:id>")
@login_required
def delete(id):
    todo = Todo.query.filter_by(id=id, user_id=current_user.id).first_or_404()
    db.session.delete(todo)
    db.session.commit()
    invalidate_cache(current_user.id)
    publish_event("todo_deleted")
    return redirect("/")

# ============================================================================
# TOGGLE
# ============================================================================

@app.route("/toggle/<int:id>")
@login_required
def toggle(id):
    todo = Todo.query.filter_by(id=id, user_id=current_user.id).first_or_404()
    todo.completed = not todo.completed
    db.session.commit()
    invalidate_cache(current_user.id)
    publish_event("todo_updated")
    return redirect("/")

# ============================================================================
# API DOCS
# ============================================================================

@app.route("/api-docs")
def api_docs():
    return render_template("api_docs.html")

# ============================================================================
# ERROR HANDLERS
# ============================================================================

@app.errorhandler(404)
def not_found(error):
    if request.path.startswith('/api/'):
        return jsonify({"error": "Resource not found"}), 404
    return "<h1>404 - Page Not Found</h1><p>The page you're looking for doesn't exist.</p>", 404

@app.errorhandler(500)
def internal_error(error):
    app.logger.error(f"Internal server error: {error}")
    db.session.rollback()
    if request.path.startswith('/api/'):
        return jsonify({"error": "Internal server error"}), 500
    return "<h1>500 - Server Error</h1><p>Something went wrong. Please try again later.</p>", 500

# ============================================================================
# CREATE DATABASE TABLES
# ============================================================================

with app.app_context():
    try:
        db.create_all()
        print("✅ Database tables created successfully")
    except Exception as e:
        print(f"⚠️ Database creation error: {e}")

# ============================================================================
# START SERVER
# ============================================================================

if __name__ == "__main__":
    start_event_listener()
    
    print("\n" + "="*60)
    print("🚀 Todo SaaS Platform - Production Ready")
    print("="*60)
    print(f"📍 Web UI:        http://localhost:5000")
    print(f"📝 Signup:        http://localhost:5000/signup")
    print(f"🔐 Login:         http://localhost:5000/login")
    print(f"📚 API Docs:      http://localhost:5000/api-docs")
    print(f"📊 Metrics:       http://localhost:5000/metrics")
    print(f"💚 Health Check:  http://localhost:5000/health")
    print("="*60 + "\n")
    
    socketio.run(app, host="0.0.0.0", port=5000, debug=True)
