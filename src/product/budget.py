"""Reuse the historical ledger/transport with a shared one-request/sec gate."""
import time
from evals.failure_driven_redesign.live_budget import BoundedClient
from evidence_core.http import _bounded_sleep, check_deadline

class RatedClient(BoundedClient):
    def complete(self, prompt, *, purpose=''):
        with self.ledger.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS d2_model_rate (id INTEGER PRIMARY KEY,next_start REAL)')
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT next_start FROM d2_model_rate WHERE id=1').fetchone()
            at=max(time.time(),row[0] if row else 0)
            db.execute('INSERT OR REPLACE INTO d2_model_rate VALUES (1,?)',(at+1,))
        _bounded_sleep(max(0,at-time.time()))
        remaining=check_deadline();original=self.timeout
        if remaining is not None:self.timeout=min(original,remaining)
        try:return super().complete(prompt,purpose=purpose)
        finally:self.timeout=original
