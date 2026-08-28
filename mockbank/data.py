"""In-memory member records for the mock core banking system.

Entirely synthetic. The records deliberately carry fields that look like
regulated data (SSN fragments, dates of birth, contact details) so that the
redaction layer has something real to prove itself against -- an automation
run touches these screens, and none of it may reach an artifact or a log.
"""

from dataclasses import dataclass, field


@dataclass
class Account:
    number: str
    kind: str
    balance: float
    status: str = "Open"


@dataclass
class Member:
    member_id: str
    name: str
    dob: str
    ssn_last4: str
    email: str
    phone: str
    branch: str
    # Some members are restricted: staff at this permission level may read them
    # but may not open accounts for them. That is a permission denial the
    # caller needs to hear about -- not a crash.
    restricted: bool = False
    accounts: list[Account] = field(default_factory=list)


MEMBERS: dict[str, Member] = {
    "100234": Member(
        member_id="100234",
        name="Dana Whitfield",
        dob="1979-04-12",
        ssn_last4="4417",
        email="d.whitfield@example.org",
        phone="(206) 555-0147",
        branch="Ballard",
        accounts=[
            Account("SAV-100234-01", "Savings", 4182.55),
            Account("CHK-100234-01", "Checking", 913.20),
        ],
    ),
    "100781": Member(
        member_id="100781",
        name="Marcus Oyelaran",
        dob="1991-11-03",
        ssn_last4="9052",
        email="m.oyelaran@example.org",
        phone="(206) 555-0192",
        branch="Fremont",
        accounts=[Account("SAV-100781-01", "Savings", 217.09)],
    ),
    "100999": Member(
        member_id="100999",
        name="Priya Raghunathan",
        dob="1965-06-28",
        ssn_last4="7731",
        email="p.raghunathan@example.org",
        phone="(206) 555-0110",
        branch="Downtown",
        restricted=True,
        accounts=[Account("SAV-100999-01", "Savings", 88301.42)],
    ),
}

# Demo credentials. Non-secret by design, but the automation must still treat
# them as sensitive: supplied per invocation, never written to an artifact.
VALID_USER = "teller01"
VALID_PASSWORD = "Passw0rd!"

MINIMUM_OPENING_DEPOSIT = 25.00

_SEQ = {"n": 2}


def next_account_number(member_id: str) -> str:
    _SEQ["n"] += 1
    return f"SAV-{member_id}-{_SEQ['n']:02d}"
