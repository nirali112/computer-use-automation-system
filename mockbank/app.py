"""Meridian Core - a stand-in for a back-office core banking console.

This is the target surface the automation drives. It is written to behave the
way the real systems in this environment behave, which means it is
deliberately awkward:

  * the console is laid out with a frameset, so there is no single document
  * pages are server-rendered tables, nested several deep
  * controls are named in the ASP.NET style (`ctl00$ContentPlaceHolder1$...`)
  * there is not a single test identifier anywhere
  * the search form labels its fields; the sub-account form does not, leaving
    the adjacent table cell as the only clue to what a field means

The flow it supports is the one the brief describes: sign on, search for a
member, read their balances, open a sub-account, reach a confirmation screen.

Two categories of non-happy-path are modelled, and the distinction between
them is the point:

  Business outcomes, produced by the application's own logic --
    an unknown member ID, a deposit below the minimum, a permission denial.
    These are legitimate answers a caller needs, not malfunctions.

  Injected faults, armed through the /_test endpoints --
    session expiry, a transient stall, an unexpected interstitial or dialog,
    an application error. These are anomalies to be recovered from or
    reported.
"""

from __future__ import annotations

import secrets
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .data import (
    MEMBERS,
    MINIMUM_OPENING_DEPOSIT,
    VALID_PASSWORD,
    VALID_USER,
    Account,
    next_account_number,
)
from .faults import FAULTS, SLOW_SECONDS

BASE = Path(__file__).parent
app = FastAPI(title="Meridian Core (mock)", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")

SESSION_COOKIE = "ASPSESSIONID"
_sessions: dict[str, str] = {}  # token -> operator id

PRODUCT_NAMES = {"SAV": "Regular Savings", "VAC": "Vacation Club", "HOL": "Holiday Club"}


# --------------------------------------------------------------------------
# rendering helpers
# --------------------------------------------------------------------------

# The frameset and the navigation frame are chrome. They do not extend the
# page shell, so they cannot raise a dialog, and consuming the fault while
# rendering them would spend it somewhere it can never fire.
CHROME_TEMPLATES = {"frameset.html", "nav.html"}


def render(request: Request, template: str, **ctx) -> HTMLResponse:
    if template not in CHROME_TEMPLATES:
        ctx.setdefault("js_confirm", FAULTS.take("js_confirm"))
    return templates.TemplateResponse(request, template, ctx)


def operator_for(request: Request) -> str | None:
    return _sessions.get(request.cookies.get(SESSION_COOKIE, ""))


class Interrupted(Exception):
    """An armed fault produced a response instead of the requested page."""

    def __init__(self, response: HTMLResponse) -> None:
        self.response = response


def gate(request: Request, *, continue_to: str) -> str:
    """Apply armed faults and the session check before a page renders.

    Returns the signed-on operator id, or raises `Interrupted` carrying the
    response that should be sent instead. Faults are applied in the order a
    real system would surface them: an expired session is discovered before
    anything else, an application error pre-empts the page, a stall delays
    it, and an interstitial is interposed in front of it.
    """
    if FAULTS.take("session_timeout"):
        _sessions.clear()
        raise Interrupted(render(request, "session_expired.html"))

    operator = operator_for(request)
    if operator is None:
        raise Interrupted(render(request, "session_expired.html"))

    if FAULTS.take("app_error"):
        raise Interrupted(
            render(request, "app_error.html", reference=f"ERR-{uuid.uuid4().hex[:8].upper()}")
        )

    if FAULTS.take("slow"):
        time.sleep(SLOW_SECONDS)

    if FAULTS.take("interstitial"):
        raise Interrupted(render(request, "interstitial.html", continue_to=continue_to))

    return operator


# --------------------------------------------------------------------------
# console shell
# --------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def frameset(request: Request):
    return render(request, "frameset.html")


@app.get("/nav", response_class=HTMLResponse)
def nav(request: Request):
    return render(request, "nav.html")


@app.get("/main")
def main(request: Request):
    return RedirectResponse("/search" if operator_for(request) else "/login", status_code=303)


# --------------------------------------------------------------------------
# sign on
# --------------------------------------------------------------------------

@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return render(request, "login.html")


@app.post("/login")
async def login_submit(request: Request):
    form = await request.form()
    user = (form.get("ctl00$txtUser") or "").strip()
    password = form.get("ctl00$txtPwd") or ""
    if user != VALID_USER or password != VALID_PASSWORD:
        return render(request, "login.html", error="Sign-on failed. Check your operator ID and password.")
    token = secrets.token_hex(16)
    _sessions[token] = user
    response = RedirectResponse("/search", status_code=303)
    response.set_cookie(SESSION_COOKIE, token, httponly=True)
    return response


@app.get("/logout")
def logout(request: Request):
    _sessions.pop(request.cookies.get(SESSION_COOKIE, ""), None)
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


# --------------------------------------------------------------------------
# member search and detail
# --------------------------------------------------------------------------

@app.get("/search", response_class=HTMLResponse)
def search_form(request: Request):
    try:
        gate(request, continue_to="/search")
    except Interrupted as stop:
        return stop.response
    return render(request, "search.html")


@app.post("/search", response_class=HTMLResponse)
async def search_submit(request: Request):
    try:
        gate(request, continue_to="/search")
    except Interrupted as stop:
        return stop.response
    form = await request.form()
    query = (form.get("ctl00$ContentPlaceHolder1$txtMemberId") or "").strip()
    member = MEMBERS.get(query)
    # An unknown member ID is a legitimate result, so it renders as one:
    # the same page, with a specific message. It is not an error page.
    return render(request, "search.html", query=query, member=member, not_found=member is None)


@app.get("/member/{member_id}", response_class=HTMLResponse)
def member_detail(request: Request, member_id: str, message: str | None = None):
    try:
        gate(request, continue_to=f"/member/{member_id}")
    except Interrupted as stop:
        return stop.response
    member = MEMBERS.get(member_id)
    if member is None:
        return render(request, "search.html", query=member_id, not_found=True)
    return render(request, "member.html", member=member, message=message)


# --------------------------------------------------------------------------
# sub-account opening
# --------------------------------------------------------------------------

@app.get("/member/{member_id}/subaccount", response_class=HTMLResponse)
def subaccount_form(request: Request, member_id: str):
    try:
        operator = gate(request, continue_to=f"/member/{member_id}/subaccount")
    except Interrupted as stop:
        return stop.response
    member = MEMBERS.get(member_id)
    if member is None:
        return render(request, "search.html", query=member_id, not_found=True)
    if member.restricted:
        return render(request, "denied.html", member_id=member_id, operator=operator)
    return render(request, "subaccount_form.html", member=member)


@app.post("/member/{member_id}/subaccount", response_class=HTMLResponse)
async def subaccount_submit(request: Request, member_id: str):
    try:
        operator = gate(request, continue_to=f"/member/{member_id}/subaccount")
    except Interrupted as stop:
        return stop.response
    member = MEMBERS.get(member_id)
    if member is None:
        return render(request, "search.html", query=member_id, not_found=True)
    if member.restricted:
        return render(request, "denied.html", member_id=member_id, operator=operator)

    form = await request.form()
    product = form.get("ctl00$ContentPlaceHolder1$ddlProduct") or "SAV"
    raw_deposit = (form.get("ctl00$ContentPlaceHolder1$txtDeposit") or "").strip()
    nickname = (form.get("ctl00$ContentPlaceHolder1$txtNickname") or "").strip()

    def invalid(message: str) -> HTMLResponse:
        return render(
            request, "subaccount_form.html",
            member=member, error=message, deposit=raw_deposit, nickname=nickname,
        )

    if not nickname:
        return invalid("Account Nickname is required.")
    try:
        deposit = float(raw_deposit.replace(",", "").lstrip("$"))
    except ValueError:
        return invalid("Initial Deposit must be a numeric amount.")
    if deposit < MINIMUM_OPENING_DEPOSIT:
        return invalid(f"Initial Deposit must be at least ${MINIMUM_OPENING_DEPOSIT:,.2f}.")

    number = next_account_number(member_id)
    member.accounts.append(Account(number, PRODUCT_NAMES.get(product, "Regular Savings"), deposit))
    return render(
        request, "subaccount_confirm.html",
        member=member,
        confirmation=f"CNF-{uuid.uuid4().hex[:10].upper()}",
        account_number=number,
        product=PRODUCT_NAMES.get(product, product),
        deposit=deposit,
    )


# --------------------------------------------------------------------------
# fault control (test harness, not part of the application under automation)
# --------------------------------------------------------------------------

@app.post("/_test/fault")
async def arm_fault(request: Request):
    body = await request.json()
    FAULTS.arm(body["kind"], int(body.get("count", 1)))
    return {"armed": FAULTS.armed()}


@app.post("/_test/reset")
def reset_faults():
    FAULTS.reset()
    return {"armed": FAULTS.armed()}


@app.get("/_test/faults")
def list_faults():
    return {"armed": FAULTS.armed()}
