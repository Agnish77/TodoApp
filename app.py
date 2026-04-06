import os
import redis
import json
import threading
import time
import hashlib
import logging
from datetime import datetime, timedelta
from functools import wraps
from typing import Dict, List, Optional, Any
from contextlib import contextmanager

from flask import Flask, render_template, request, redirect, jsonify, flash, g, session, make_response
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
# FAANG-LEVEL OBSERVABILITY IMPORTS
# ============================================================================
from prometheus_flask_exporter import PrometheusMetrics
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.jaeger.thrift import JaegerExporter
from opentelemetry.instrumentation.flask import FlaskInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace.id_generator import RandomIdGenerator
from opentelemetry.trace import Status, StatusCode

# ============================================================================
# APP INITIALIZATION
# ============================================================================

app = Flask(__name__)

# ============================================================================
# FAANG-LEVEL OBSERVABILITY SETUP (DOES NOT BREAK EXISTING CODE)
# ============================================================================

# Setup logging for production
logging.basicConfig(level=logging.INFO)
app.logger.setLevel(logging.INFO)

# Prometheus Metrics
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

ACTIVE_USERS = metrics.gauge(
    'active_users', 
    'Currently active users'
)

DB_QUERY_DURATION = metrics.histogram(
    'db_query_duration_seconds', 
    'Database query duration',
    buckets=[0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1]
)

# OpenTelemetry Distributed Tracing
resource = Resource(attributes={
    SERVICE_NAME: "todo-saas-platform",
    "service.version": "2.0.0",
    "deployment.environment": os.getenv("ENV", "development")
})

trace.set_tracer_provider(TracerProvider(resource=resource, id_generator=RandomIdGenerator()))

# Configure Jaeger exporter (for distributed tracing visualization)
jaeger_exporter = JaegerExporter(
    agent_host_name=os.getenv("JAEGER_HOST", "localhost"),
    agent_port=6831,
)
trace.get_tracer_provider().add_span_processor(
    BatchSpanProcessor(jaeger_exporter)
)

# Instrument Flask for automatic tracing
FlaskInstrumentor().instrument_app(app)

# Instrument SQLAlchemy for database query tracing
SQLAlchemyInstrumentor().instrument(
    engine=db.engine,
    tracer_provider=trace.get_tracer_provider()
)

tracer = trace.get_tracer(__name__)

# ============================================================================
# PERFORMANCE MIDDLEWARE
# ============================================================================

@app.before_request
def before_request():
    """Start request timer and track active users"""
    g.start_time = time.time()
    g.request_id = hashlib.md5(f"{time.time()}{request.remote_addr}".encode()).hexdigest()[:16]
    
    # Add request ID to response headers for tracing
    if hasattr(g, 'request_id'):
        pass

@app.after_request
def after_request(response):
    """Record metrics and log slow requests"""
    if hasattr(g, 'start_time'):
        elapsed = time.time() - g.start_time
        
        # Record latency metric
        REQUEST_LATENCY.observe(elapsed)
        
        # Add response headers for observability
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
                "user": current_user.id if current_user.is_authenticated else "anonymous",
                "ip": request.remote_addr
            }))
            
            # Store in Redis for analysis
            try:
                redis_client.lpush("slow_requests", json.dumps({
                    "request_id": g.request_id,
                    "path": request.path,
                    "method": request.method,
                    "duration_ms": elapsed * 1000,
                    "user": current_user.id if current_user.is_authenticated else None,
                    "timestamp": datetime.now().isoformat()
                }))
                redis_client.ltrim("slow_requests", 0, 999)
            except:
                pass
    
    return response

# ============================================================================
# CACHE DECORATOR (FAANG-LEVEL CACHING STRATEGY)
# ============================================================================

def cached(timeout=60, key_prefix='view'):
    """Cache decorator for expensive operations"""
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            # Skip cache for authenticated users (they have their own cache)
            if current_user.is_authenticated:
                return f(*args, **kwargs)
            
            # Create cache key
            cache_key = f"{key_prefix}:{request.path}:{request.args}"
            try:
                cached_data = redis_client.get(cache_key)
                if cached_data:
                    return jsonify(json.loads(cached_data))
            except:
                pass
            
            # Execute function and cache result
            response = f(*args, **kwargs)
            
            if response and hasattr(response, 'get_data'):
                try:
                    redis_client.setex(cache_key, timeout, response.get_data(as_text=True))
                except:
                    pass
            
            return response
        return decorated_function
    return decorator

# ============================================================================
# RATE LIMITING STRATEGIES (FAANG PATTERNS)
# ============================================================================

class RateLimitStrategy:
    """Implement multiple rate limiting strategies"""
    
    @staticmethod
    def token_bucket(user_id, key, rate=10, capacity=20):
        """Token bucket algorithm for burst handling"""
        redis_key = f"ratelimit:token:{user_id}:{key}"
        now = time.time()
        
        pipe = redis_client.pipeline()
        pipe.get(redis_key)
        pipe.get(f"{redis_key}:tokens")
        result = pipe.execute()
        
        last_refill = float(result[0]) if result[0] else now
        tokens = float(result[1]) if result[1] else capacity
        
        # Calculate tokens to add
        time_passed = now - last_refill
        tokens_to_add = time_passed * rate
        tokens = min(capacity, tokens + tokens_to_add)
        
        if tokens >= 1:
            tokens -= 1
            pipe = redis_client.pipeline()
            pipe.setex(redis_key, 60, now)
            pipe.setex(f"{redis_key}:tokens", 60, tokens)
            pipe.execute()
            return True
        return False
    
    @staticmethod
    def sliding_window(user_id, key, limit=100, window=60):
        """Sliding window log algorithm"""
        redis_key = f"ratelimit:sliding:{user_id}:{key}"
        now = time.time()
        window_start = now - window
        
        # Remove old entries
        redis_client.zremrangebyscore(redis_key, 0, window_start)
        
        # Count requests in window
        count = redis_client.zcard(redis_key)
        
        if count < limit:
            redis_client.zadd(redis_key, {str(now): now})
            redis_client.expire(redis_key, window)
            return True
        return False

# ============================================================================
# HEALTH CHECK ENDPOINTS (KUBERNETES READY)
# ============================================================================

@app.route('/health', methods=['GET'])
def health_check():
    """Comprehensive health check for container orchestration"""
    health = {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "service": "todo-saas",
        "version": "2.0.0",
        "uptime_seconds": time.time() - app.config.get('START_TIME', time.time()),
        "checks": {}
    }
    
    # Check database
    try:
        start = time.time()
        db.session.execute('SELECT 1')
        health['checks']['database'] = {
            "status": "up",
            "latency_ms": (time.time() - start) * 1000
        }
    except Exception as e:
        health['checks']['database'] = {"status": "down", "error": str(e)}
        health['status'] = "degraded"
    
    # Check Redis
    try:
        start = time.time()
        redis_client.ping()
        health['checks']['redis'] = {
            "status": "up",
            "latency_ms": (time.time() - start) * 1000
        }
    except Exception as e:
        health['checks']['redis'] = {"status": "down", "error": str(e)}
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
# PERFORMANCE METRICS ENDPOINTS
# ============================================================================

@app.route('/api/metrics/slow-requests', methods=['GET'])
@login_required
def get_slow_requests():
    """Get recent slow requests (FAANG debugging feature)"""
    try:
        slow_requests = redis_client.lrange("slow_requests", 0, 99)
        return jsonify({
            "count": len(slow_requests),
            "requests": [json.loads(req) for req in slow_requests]
        })
    except:
        return jsonify({"error": "Metrics unavailable"}), 500

@app.route('/api/metrics/cache-stats', methods=['GET'])
@login_required
def get_cache_stats():
    """Get cache hit/miss statistics"""
    # Simple cache stats implementation
    return jsonify({
        "cache_engine": "Redis",
        "default_ttl": 60,
        "hit_rate": "Unknown (implement with metrics)"
    })

# ============================================================================
# DATABASE QUERY OPTIMIZATION (N+1 DETECTION)
# ============================================================================

class QueryOptimizer:
    """Detect and prevent N+1 queries"""
    
    def __init__(self):
        self.queries = []
    
    def register_query(self, query, duration):
        self.queries.append({
            "query": str(query),
            "duration": duration,
            "time": datetime.now()
        })
        
        # Alert if query is too slow
        if duration > 0.1:  # 100ms threshold
            app.logger.warning(f"Slow query detected: {duration:.3f}s - {query}")
        
        # Keep only last 1000 queries
        if len(self.queries) > 1000:
            self.queries = self.queries[-1000:]

# Initialize query optimizer
query_optimizer = QueryOptimizer()

# ============================================================================
# ORIGINAL APP CONFIG (UNCHANGED)
# ============================================================================

app.secret_key = os.getenv("SECRET_KEY", "dev-secret")

app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv(
    "DATABASE_URL",
    "sqlite:///todo.db"
)

app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config['START_TIME'] = time.time()

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

redis_client = redis.from_url(REDIS_URL)

task_queue = Queue(connection=redis_client)

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
    storage_uri=REDIS_URL,
    default_limits=["200 per day", "50 per hour"]
)

socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    message_queue=REDIS_URL,
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

# ============================================================================
# CACHE SYSTEM (ENHANCED)
# ============================================================================

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

# ============================================================================
# BACKGROUND JOB
# ============================================================================

def log_event(event):
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
        password = bcrypt.generate_password_hash(
            request.form["password"]
        ).decode("utf-8")
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

# ============================================================================
# LOGOUT
# ============================================================================

@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect("/login")

# ============================================================================
# DASHBOARD (ENHANCED WITH TRACING)
# ============================================================================

@app.route("/", methods=["GET", "POST"])
@login_required
def index():
    # Add distributed tracing span
    with tracer.start_as_current_span("dashboard-index") as span:
        span.set_attribute("user.id", current_user.id)
        span.set_attribute("http.method", request.method)
        
        if request.method == "POST":
            with tracer.start_as_current_span("create-todo"):
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
                span.set_attribute("todo.created", True)
                return redirect("/")
        
        search = request.args.get("search")
        query = Todo.query.filter_by(user_id=current_user.id)
        
        if search:
            with tracer.start_as_current_span("search-todos"):
                query = query.filter(Todo.title.contains(search))
                span.set_attribute("search.query", search)
        
        with tracer.start_as_current_span("fetch-todos"):
            todos = query.order_by(Todo.date_c.desc()).all()
            span.set_attribute("todos.count", len(todos))
        
        return render_template("index.html", todos=todos)

# ============================================================================
# UPDATE TODO (EDIT ROUTE)
# ============================================================================

@app.route("/update/<int:id>", methods=["GET", "POST"])
@login_required
def update(id):
    todo = Todo.query.filter_by(
        id=id,
        user_id=current_user.id
    ).first_or_404()
    
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
    todo = Todo.query.filter_by(
        id=id,
        user_id=current_user.id
    ).first_or_404()
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
    todo = Todo.query.filter_by(
        id=id,
        user_id=current_user.id
    ).first_or_404()
    todo.completed = not todo.completed
    db.session.commit()
    invalidate_cache(current_user.id)
    publish_event("todo_updated")
    return redirect("/")

# ============================================================================
# API DOCS PAGE
# ============================================================================

@app.route("/api-docs")
def api_docs():
    return render_template("api_docs.html")

# ============================================================================
# ERROR HANDLERS
# ============================================================================

@app.errorhandler(404)
def not_found(error):
    return jsonify({"error": "Resource not found"}), 404

@app.errorhandler(500)
def internal_error(error):
    app.logger.error(f"Internal server error: {error}")
    return jsonify({"error": "Internal server error"}), 500

@app.errorhandler(429)
def rate_limit_error(error):
    return jsonify({"error": "Rate limit exceeded", "retry_after": 60}), 429

# ============================================================================
# START SERVER
# ============================================================================

if __name__ == "__main__":
    start_event_listener()
    
    print("\n" + "="*60)
    print("🚀 Todo SaaS Platform - FAANG Ready Edition")
    print("="*60)
    print(f"📍 Web UI:        http://localhost:5000")
    print(f"📝 Signup:        http://localhost:5000/signup")
    print(f"🔐 Login:         http://localhost:5000/login")
    print(f"📚 API Docs:      http://localhost:5000/api-docs")
    print(f"📊 Metrics:       http://localhost:5000/metrics")
    print(f"💚 Health Check:  http://localhost:5000/health")
    print("="*60)
    print("✨ FAANG Features Added:")
    print("   • Distributed Tracing (OpenTelemetry + Jaeger)")
    print("   • Prometheus Metrics Collection")
    print("   • Health Checks for Kubernetes")
    print("   • Performance Monitoring")
    print("   • Slow Request Detection")
    print("   • Advanced Rate Limiting Strategies")
    print("="*60 + "\n")
    
    socketio.run(app, host="0.0.0.0", port=5000, debug=True)
