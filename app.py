
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
# APP INITIALIZATION
# ============================================================================

app = Flask(__name__)

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
# SIMPLE PERFORMANCE MIDDLEWARE (No external dependencies)
# ============================================================================

@app.before_request
def before_request():
    """Start request timer"""
    g.start_time = time.time()

@app.after_request
def after_request(response):
    """Log slow requests"""
    if hasattr(g, 'start_time'):
        elapsed = time.time() - g.start_time
        # Add response header
        response.headers['X-Response-Time'] = f"{elapsed*1000:.2f}ms"
        # Log slow requests (>1 second)
        if elapsed > 1:
            app.logger.warning(f'Slow request: {request.path} took {elapsed:.3f}s')
    return response

# ============================================================================
# HEALTH CHECK ENDPOINTS
# ============================================================================

@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    return jsonify({
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "service": "todo-saas"
    }), 200

@app.route('/ready', methods=['GET'])
def readiness_probe():
    """Readiness probe"""
    return jsonify({"status": "ready"}), 200

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
    return "<h1>404 - Page Not Found</h1>", 404

@app.errorhandler(500)
def internal_error(error):
    app.logger.error(f"Internal server error: {error}")
    db.session.rollback()
    if request.path.startswith('/api/'):
        return jsonify({"error": "Internal server error"}), 500
    return "<h1>500 - Server Error</h1>", 500

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
    print(f"💚 Health Check:  http://localhost:5000/health")
    print("="*60 + "\n")
    
    socketio.run(app, host="0.0.0.0", port=5000, debug=True)
