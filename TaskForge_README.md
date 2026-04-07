# 🚀 TaskForge - Production Task Management System

[![Deploy on
Render](https://img.shields.io/badge/Deploy%20on-Render-blue?style=flat-square&logo=render)](https://todoapp-twlc.onrender.com)
[![GitHub
CI/CD](https://img.shields.io/badge/CI%2FCD-GitHub%20Actions-green?style=flat-square&logo=github-actions)](https://github.com/Agnish77/TodoApp/actions)
[![Python](https://img.shields.io/badge/Python-3.11-blue?style=flat-square&logo=python)](https://www.python.org/)
[![Flask](https://img.shields.io/badge/Flask-2.3-black?style=flat-square&logo=flask)](https://flask.palletsprojects.com/)
[![Docker](https://img.shields.io/badge/Docker-ready-blue?style=flat-square&logo=docker)](https://www.docker.com/)

> A production‑ready task management platform designed with real‑world
> backend engineering practices including authentication systems,
> real‑time updates, caching, background workers, and containerized
> deployment.

------------------------------------------------------------------------

# 📊 Live Demo

**URL:** https://todoapp-twlc.onrender.com

Demo credentials

Username: `demo_user`\
Password: `Demo@123`

------------------------------------------------------------------------

# ✨ Key Features

## 🔐 Authentication & Security

-   Dual authentication architecture using **Flask‑Login sessions** for
    web clients and **JWT tokens** for REST APIs
-   Secure password hashing using **bcrypt**
-   API **rate limiting** to prevent abuse
-   Strong password validation and secure session handling

## 📝 Task Management

-   Full **CRUD operations** for user tasks
-   **User-level data isolation** ensuring tasks are accessible only to
    their owners
-   **Search and filtering** functionality
-   **Task completion toggle** for status management

## ⚡ Real‑Time Updates

-   **WebSocket based task synchronization** using Flask‑SocketIO
-   Redis **Pub/Sub** for event broadcasting across clients
-   Instant UI updates without page refresh

## 🚀 Performance & Scalability

-   **Redis caching layer** with TTL-based cache invalidation
-   **Paginated REST APIs** for efficient data retrieval
-   **Background workers** using Redis Queue (RQ) for asynchronous
    processing
-   Response‑time monitoring via HTTP headers

## 🐳 DevOps & Deployment

-   Containerized using **Docker**
-   Production server using **Gunicorn + Eventlet workers**
-   Automated **CI/CD pipeline with GitHub Actions**
-   Deployed on **Render cloud platform** with auto-deploy

------------------------------------------------------------------------

# 🏗 Architecture

Client\
↓\
Gunicorn (Eventlet Workers)\
↓\
Flask Application (MVC Architecture)\
↓\
Redis Cache / PostgreSQL Database / RQ Worker Queue

------------------------------------------------------------------------

# 🧰 Tech Stack

Backend: Flask, Python\
Database: PostgreSQL / SQLite\
Caching: Redis\
Queue: Redis Queue (RQ)\
Real‑time: Flask‑SocketIO / WebSockets\
Authentication: Flask‑Login, JWT, bcrypt\
Server: Gunicorn + Eventlet\
Containerization: Docker\
CI/CD: GitHub Actions\
Cloud: Render

------------------------------------------------------------------------

# 🚀 Quick Start

Clone the repository

git clone https://github.com/Agnish77/TodoApp.git\
cd TodoApp

Create virtual environment

python -m venv venv

Activate environment

Linux/Mac: source venv/bin/activate

Windows: venv`\Scripts`{=tex}`\activate`{=tex}

Install dependencies

pip install -r requirements.txt

Run application

python app.py

------------------------------------------------------------------------

# 🐳 Docker Deployment

Build image

docker build -t taskforge .

Run container

docker run -p 5000:5000 taskforge

------------------------------------------------------------------------

# 📊 API Endpoints

POST /api/login -- JWT login\
GET /api/todos -- Retrieve tasks\
POST /api/todos -- Create task\
GET /health -- Health check

------------------------------------------------------------------------

# 📁 Project Structure

TodoApp/

app.py -- application entry point\
data.py -- database initialization\
model.py -- SQLAlchemy models\
templates/ -- HTML templates\
static/ -- static assets\
tests/ -- automated tests\
Dockerfile -- container configuration\
.github/workflows -- CI/CD pipeline

------------------------------------------------------------------------

# 👨‍💻 Author

Agnish Paul\
GitHub: https://github.com/Agnish77
