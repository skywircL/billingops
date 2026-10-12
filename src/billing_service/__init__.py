"""Independent simulated Billing service.

Its own process, its own SQLite file, its own transaction boundaries. The
application only ever talks to it over HTTP. Nothing in here imports from
``billingops``; the two sides share a wire format and nothing else.
"""
