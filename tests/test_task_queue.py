import asyncio

from api.queue.task_queue import Task, TaskPriority, TaskQueue


def test_cancelled_user_task_runs_cleanup_callback():
    async def scenario():
        queue = TaskQueue(max_concurrent=1)
        cleanup_ran = asyncio.Event()

        async def task_body():
            raise AssertionError("cancelled task body must not run")

        async def cleanup():
            cleanup_ran.set()

        await queue.add_task(
            Task(
                name="user-download",
                user_id="5201:student",
                func=task_body,
                priority=TaskPriority.LOW,
                on_cancel=cleanup,
            )
        )
        await queue.cancel_user_tasks("5201:student")
        await queue.start()
        try:
            await asyncio.wait_for(cleanup_ran.wait(), timeout=2)
        finally:
            await queue.stop(wait=False)

    asyncio.run(scenario())
