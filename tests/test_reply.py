from __future__ import annotations

from fastapi.testclient import TestClient


def reply(client: TestClient, conversation: str, message: str, turn: int = 2):
    return client.post(
        "/v1/reply",
        json={
            "conversation_id": conversation,
            "merchant_id": "m_001",
            "customer_id": None,
            "from_role": "merchant",
            "message": message,
            "received_at": "2026-08-23T10:00:00Z",
            "turn_number": turn,
        },
    )


def test_commitment_switches_to_action_not_qualification(client: TestClient):
    response = reply(client, "conv-intent", "Ok lets do it. Whats next?")
    assert response.status_code == 200
    data = response.json()
    assert data["action"] == "send"
    assert any(word in data["body"].lower() for word in ("done", "draft", "next", "confirm"))
    assert not any(phrase in data["body"].lower() for phrase in ("would you", "do you", "can you tell"))


def test_hinglish_commitment_is_actioned(client: TestClient):
    data = reply(client, "conv-hinglish", "Haan theek hai, kar do").json()
    assert data["action"] == "send"
    assert "draft" in data["body"].lower()


def test_auto_reply_memory_survives_changed_conversation_ids(client: TestClient):
    message = "Thank you for contacting us! Our team will respond shortly."
    first = reply(client, "conv-auto-1", message, 2).json()
    second = reply(client, "conv-auto-2", message, 3).json()
    third = reply(client, "conv-auto-3", message, 4).json()
    assert first["action"] == "wait"
    assert second["action"] == "wait"
    assert third["action"] == "end"


def test_stop_ends_and_creates_permanent_cooldown(client: TestClient):
    data = reply(client, "conv-stop", "Stop messaging me. This is useless spam.").json()
    assert data["action"] == "end"
    assert "won't message" in data["body"].lower()
    assert client.app.state.store.is_cooled_down("m_001", "9999-01-01T00:00:00Z")


def test_pause_returns_wait(client: TestClient):
    data = reply(client, "conv-wait", "I am busy, remind me tomorrow").json()
    assert data["action"] == "wait"
    assert data["wait_seconds"] == 86_400


def test_off_topic_does_not_fabricate(client: TestClient):
    data = reply(client, "conv-offtopic", "Can you calculate my GST liability?").json()
    assert data["action"] == "send"
    assert "don't have verified context" in data["body"].lower()


def test_ambiguous_message_has_bounded_choices(client: TestClient):
    data = reply(client, "conv-ambiguous", "hmm").json()
    assert data["action"] == "send"
    assert all(word in data["body"] for word in ("YES", "LATER", "STOP"))
