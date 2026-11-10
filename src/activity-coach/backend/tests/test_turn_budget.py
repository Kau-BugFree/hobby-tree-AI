import importlib
import pytest


@pytest.mark.parametrize('kind,limit', [('decision',6),('retrieval',3),('generation',2),('verification',2)])
def test_budget_last_allowed_and_first_rejected(kind, limit):
    module = importlib.import_module('app.turn_budget')
    budget = module.TurnBudget()
    for _ in range(limit):
        budget.consume(kind)
    with pytest.raises(module.BudgetExceeded):
        budget.consume(kind)
    assert budget.counts[kind] == limit


def test_deadline_is_shared_and_not_reset():
    module = importlib.import_module('app.turn_budget')
    budget = module.TurnBudget(seconds=0)
    with pytest.raises(module.BudgetExceeded):
        budget.consume('decision')


def test_inflight_model_call_is_counted_on_cancellation():
    import asyncio
    async def run():
        module=importlib.import_module('app.turn_budget')
        budget=module.TurnBudget()
        async def slow():
            await asyncio.sleep(1)
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(.001):
                await budget.model_call('decision',slow)
        assert budget.llm_calls==1 and budget.counts['decision']==1
    asyncio.run(run())
