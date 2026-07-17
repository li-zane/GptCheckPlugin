import asyncio
import unittest

from app.services.workflow_coordination import WorkflowCoordinator


class WorkflowCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_background_usage_waits_for_active_upstream_batch(self) -> None:
        coordinator = WorkflowCoordinator()
        async with coordinator.upstream_batch():
            waiter = asyncio.create_task(
                coordinator.wait_for_upstream_idle(grace_seconds=0)
            )
            await asyncio.sleep(0)
            self.assertFalse(waiter.done())

        await asyncio.wait_for(waiter, timeout=1)

    async def test_grace_window_observes_new_upstream_batch(self) -> None:
        coordinator = WorkflowCoordinator()
        waiter = asyncio.create_task(
            coordinator.wait_for_upstream_idle(grace_seconds=0.02)
        )
        await asyncio.sleep(0.005)
        async with coordinator.upstream_batch():
            await asyncio.sleep(0.03)
            self.assertFalse(waiter.done())

        await asyncio.wait_for(waiter, timeout=1)


if __name__ == "__main__":
    unittest.main()
