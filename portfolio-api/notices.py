# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""notices.py -- the standard wording for the app's notices (RV-T1 / TASK-RV-044). One place, so a notice that has to travel with copied
text, an e-mail or a push message says the same thing everywhere. static/index.html repeats the constants it needs (it cannot import this);
test_notices.py fails if the two copies drift apart."""

CLOSING = "Informational only, produced by a personal tool. Not financial, tax or legal advice."     # text that leaves the app
PAPER = "Hypothetical paper portfolio. No real orders are placed."                                    # Strategist pages and reports
ESTIMATE = "Estimates from published tables. Not tax or legal advice."                                 # tax, Roth, ACA, Social Security, estate, insurance, long-term care
LEAD = "A research lead, not a recommendation."                                                        # beside a conviction score
NAME = "Fiduciary is a product name. Using it creates no fiduciary or advisory relationship."         # README and sign-in page
ACK_TITLE = "Before you start"
ACK_TEXT = ("This is a personal, informational tool. It organises financial information and shows estimates and research leads. "
            "It is not a financial advisor, tax preparer or lawyer, and using it creates no advisory relationship. "
            "The AI features can be wrong: check anything important against the source and with a professional before acting on it. "
            "Nothing in it places an order or moves money.")


def with_notice(body, limit=300):
    """`body` followed by the closing line; the body is shortened (never the line) to fit `limit` characters."""
    tail = "\n\n" + CLOSING
    body = str(body or "")
    room = max(0, limit - len(tail))
    if len(body) > room:
        body = body[:max(0, room - 1)].rstrip() + "…"
    return body + tail
