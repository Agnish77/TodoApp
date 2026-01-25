def get_token(client):
    response = client.post(
        "/api/login",
        json={"username": "testuser", "password": "testpass"}
    )
    return response.json["access_token"]


def test_get_todos_authorized(client):
    token = get_token(client)

    response = client.get(
        "/api/todos",
        headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200
    assert "todos" in response.json


def test_get_todos_unauthorized(client):
    response = client.get("/api/todos")
    assert response.status_code == 401
