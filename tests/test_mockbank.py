"""The mock target's own behaviour.

These tests pin the distinction the rest of the system is built on: which
responses are the application answering correctly, and which are it
malfunctioning. If this line ever moves, the replay engine's result contract
is wrong, so it is worth asserting directly.
"""

import pytest
from fastapi.testclient import TestClient

from mockbank.app import app
from mockbank.faults import FAULTS

SIGNON = {"ctl00$txtUser": "teller01", "ctl00$txtPwd": "Passw0rd!"}
SEARCH_FIELD = "ctl00$ContentPlaceHolder1$txtMemberId"


@pytest.fixture
def client():
    FAULTS.reset()
    with TestClient(app) as c:
        c.post("/login", data=SIGNON)
        yield c
    FAULTS.reset()


def subaccount(client, member_id, deposit="100.00", nickname="Holiday", product="SAV"):
    return client.post(
        f"/member/{member_id}/subaccount",
        data={
            "ctl00$ContentPlaceHolder1$ddlProduct": product,
            "ctl00$ContentPlaceHolder1$txtDeposit": deposit,
            "ctl00$ContentPlaceHolder1$txtNickname": nickname,
        },
    )


# -- sign on ---------------------------------------------------------------

def test_bad_credentials_are_rejected():
    with TestClient(app) as c:
        r = c.post("/login", data={"ctl00$txtUser": "teller01", "ctl00$txtPwd": "nope"})
        assert "Sign-on failed" in r.text


def test_pages_require_a_session():
    with TestClient(app) as c:
        assert "Session Expired" in c.get("/search").text


# -- business outcomes: the application answering correctly -----------------

def test_known_member_is_found(client):
    r = client.post("/search", data={SEARCH_FIELD: "100234"})
    assert "Dana Whitfield" in r.text and "No record found" not in r.text


def test_unknown_member_is_a_result_not_an_error(client):
    r = client.post("/search", data={SEARCH_FIELD: "99999"})
    assert r.status_code == 200
    assert "No record found for member ID 99999" in r.text


def test_balance_is_present_on_the_detail_screen(client):
    assert "$4,182.55" in client.get("/member/100234").text


def test_deposit_below_minimum_is_rejected_with_a_reason(client):
    assert "Initial Deposit must be at least $25.00" in subaccount(client, "100234", deposit="5").text


def test_non_numeric_deposit_is_rejected_with_a_reason(client):
    assert "Initial Deposit must be a numeric amount" in subaccount(client, "100234", deposit="abc").text


def test_missing_nickname_is_rejected_with_a_reason(client):
    assert "Account Nickname is required" in subaccount(client, "100234", nickname="").text


def test_restricted_member_cannot_have_accounts_opened(client):
    r = subaccount(client, "100999")
    assert r.status_code == 200
    assert "not authorised" in r.text


def test_valid_request_reaches_the_confirmation_screen(client):
    r = subaccount(client, "100234", deposit="150.00", nickname="Summer Trip", product="VAC")
    assert "Sub-Account Request Confirmed" in r.text
    assert "CNF-" in r.text and "Vacation Club" in r.text


# -- injected faults: the application malfunctioning ------------------------

def test_session_timeout_interrupts_and_is_consumed_once(client):
    FAULTS.arm("session_timeout")
    assert "Your session has expired" in client.get("/search").text
    client.post("/login", data=SIGNON)
    assert "Member Search" in client.get("/search").text


def test_interstitial_is_interposed_then_clears(client):
    FAULTS.arm("interstitial")
    interrupted = client.get("/search")
    assert "System Notice" in interrupted.text and "Continue" in interrupted.text
    assert "Member Search" in client.get("/search").text


def test_app_error_surfaces_a_reference(client):
    FAULTS.arm("app_error")
    r = client.get("/search")
    assert "Application Error" in r.text and "ERR-" in r.text


def test_unexpected_dialog_is_injected_into_the_page(client):
    FAULTS.arm("js_confirm")
    assert "confirm(" in client.get("/search").text


def test_faults_can_be_armed_for_several_renders(client):
    FAULTS.arm("app_error", 2)
    assert "Application Error" in client.get("/search").text
    assert "Application Error" in client.get("/search").text
    assert "Member Search" in client.get("/search").text
