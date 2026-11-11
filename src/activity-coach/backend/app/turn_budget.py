"""One deadline and explicit operation counts for a logical turn."""
import time


class BudgetExceeded(Exception):
    def __init__(self, reason: str = 'budget_exhausted'):
        self.reason = reason
        super().__init__(reason)


class TurnBudget:
    limits = {'decision': 6, 'retrieval': 3, 'generation': 2, 'verification': 2}

    def __init__(self, seconds: float = 60):
        self.deadline = time.monotonic() + seconds
        self.counts = dict.fromkeys(self.limits, 0)
        self.llm_calls = 0
        self.usage = {}

    def remaining_seconds(self) -> float:
        return max(0, self.deadline - time.monotonic())

    def consume(self, kind: str) -> None:
        if not self.remaining_seconds() or self.counts[kind] >= self.limits[kind]:
            raise BudgetExceeded()
        self.counts[kind] += 1

    def record(self, result: dict) -> None:
        self.llm_calls += result.get('llm_calls', 0)
        for name, value in result.get('usage', {}).items():
            if isinstance(value, (int, float)):
                self.usage[name] = self.usage.get(name, 0) + value

    async def model_call(self, kind: str, call, *args) -> dict:
        self.consume(kind)
        self.llm_calls += 1  # Retain the attempted call if cancellation prevents a response.
        result = await call(*args)
        self.llm_calls -= 1
        self.record(result)
        return result

    def diagnostics(self) -> dict:
        return {**{kind + '_calls': count for kind, count in self.counts.items()},
                'llm_calls': self.llm_calls, 'usage': dict(self.usage)}
