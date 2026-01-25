def test_api_login_success(client):
    response = client.post(
        "/api/login",
        json={"username": "testuser", "password": "testpass"}
    )

    assert response.status_code == 200
    assert "access_token" in response.json


def test_api_login_failure(client):
    response = client.post(
        "/api/login",
        json={"username": "testuser", "password": "wrongpass"}
    )

    assert response.status_code == 401
