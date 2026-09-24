import asyncio

from api.queue.task_queue import Task, TaskPriority, TaskQueue


def test_cancelled_user_task_runs_cleanup_callback():
    async def scenario():
        queue = TaskQueue(max_concurrent=1)
        cleanup_ran = asyncio.Event()

        async def task_body(*_args):
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
        await queue.allow_user_tasks("5201:student")
        await queue.start()
        try:
            await asyncio.wait_for(cleanup_ran.wait(), timeout=2)
        finally:
            await queue.stop(wait=False)

    asyncio.run(scenario())


def test_cancel_user_tasks_purges_completed_task_arguments():
    async def scenario():
        queue = TaskQueue(max_concurrent=1)

        async def task_body(*_args):
            return None

        task = Task(
            name="profile-fetch",
            user_id="5201:student",
            args=("5201:student", "5201", "student", "session-secret"),
            func=task_body,
            priority=TaskPriority.LOW,
        )
        await queue.add_task(task)
        await queue.start()
        try:
            await asyncio.wait_for(queue._queue.join(), timeout=2)
            assert task.task_id in queue._completed_tasks
            await queue.cancel_user_tasks("5201:student")
            assert task.task_id not in queue._completed_tasks
        finally:
            await queue.stop(wait=False)

    asyncio.run(scenario())
