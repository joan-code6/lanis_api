"""
Background Task Queue System for Schulportal Hessen API.

Features:
- Runs alongside the main API server without impacting performance
- Resource-limited execution with configurable max concurrent tasks
- Priority-based task scheduling
- Task status tracking and retry logic
- Async-first design for non-blocking operation

Usage:
    from api.queue import task_queue, Task, TaskPriority
    
    # Add a task
    task = Task(
        name="fetch_user_data",
        func=my_async_func,
        args=(arg1,),
        kwargs={"key": "value"},
        priority=TaskPriority.NORMAL
    )
    await task_queue.add_task(task)
    
    # Or use the decorator
    @task_queue.background_task(priority=TaskPriority.LOW)
    async def my_background_func():
        pass
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum
from typing import Any, Callable, Coroutine, Dict, List, Optional
from uuid import uuid4

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("task_queue")


class TaskPriority(IntEnum):
    """Task priority levels. Lower values = higher priority."""
    HIGH = 1
    NORMAL = 2
    LOW = 3


class TaskStatus:
    """Task execution status."""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    RETRYING = "retrying"


@dataclass(order=True)
class Task:
    """
    Represents a background task to be executed.
    
    Attributes:
        name: Human-readable task name for logging
        func: Async callable to execute
        args: Positional arguments for the function
        kwargs: Keyword arguments for the function
        priority: Task priority (lower = higher priority)
        max_retries: Maximum retry attempts on failure
        retry_delay: Seconds to wait between retries
    """
    priority: TaskPriority = field(compare=True)
    created_at: datetime = field(default_factory=datetime.utcnow, compare=True)
    task_id: str = field(default_factory=lambda: uuid4().hex, compare=False)
    name: str = field(default="unnamed_task", compare=False)
    func: Callable[..., Coroutine[Any, Any, Any]] = field(default=None, compare=False)
    args: tuple = field(default_factory=tuple, compare=False)
    kwargs: Dict[str, Any] = field(default_factory=dict, compare=False)
    max_retries: int = field(default=3, compare=False)
    retry_delay: float = field(default=1.0, compare=False)
    
    # Runtime state
    status: str = field(default=TaskStatus.PENDING, compare=False)
    retry_count: int = field(default=0, compare=False)
    result: Any = field(default=None, compare=False)
    error: Optional[str] = field(default=None, compare=False)
    started_at: Optional[datetime] = field(default=None, compare=False)
    completed_at: Optional[datetime] = field(default=None, compare=False)


class TaskQueue:
    """
    Async background task queue with resource management.
    
    Manages a pool of concurrent workers that process tasks from a priority queue.
    Designed to run alongside the main API server without impacting performance.
    
    Attributes:
        max_concurrent: Maximum number of tasks running simultaneously
        max_queue_size: Maximum pending tasks (0 = unlimited)
    """
    
    def __init__(
        self,
        max_concurrent: int = 2,
        max_queue_size: int = 100,
    ) -> None:
        self.max_concurrent = max_concurrent
        self.max_queue_size = max_queue_size
        
        self._queue: asyncio.PriorityQueue[Task] = asyncio.PriorityQueue(maxsize=max_queue_size)
        self._active_tasks: Dict[str, Task] = {}
        self._completed_tasks: Dict[str, Task] = {}
        self._workers: List[asyncio.Task] = []
        self._running = False
        self._lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(max_concurrent)
    
    async def start(self) -> None:
        """Start the task queue workers."""
        if self._running:
            logger.warning("Task queue already running")
            return
        
        self._running = True
        logger.info(f"Starting task queue with {self.max_concurrent} workers")
        
        # Start worker tasks
        for i in range(self.max_concurrent):
            worker = asyncio.create_task(self._worker(i))
            self._workers.append(worker)
    
    async def stop(self, wait: bool = True, timeout: float = 30.0) -> None:
        """
        Stop the task queue.
        
        Args:
            wait: If True, wait for pending tasks to complete
            timeout: Maximum seconds to wait for tasks
        """
        if not self._running:
            return
        
        logger.info("Stopping task queue...")
        self._running = False
        
        if wait:
            # Wait for queue to drain with timeout
            try:
                await asyncio.wait_for(self._queue.join(), timeout=timeout)
            except asyncio.TimeoutError:
                logger.warning(f"Task queue shutdown timed out after {timeout}s")
        
        # Cancel all workers
        for worker in self._workers:
            worker.cancel()
        
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        logger.info("Task queue stopped")
    
    async def add_task(self, task: Task) -> str:
        """
        Add a task to the queue.
        
        Args:
            task: Task to add
            
        Returns:
            Task ID for tracking
            
        Raises:
            asyncio.QueueFull: If queue is at max capacity
        """
        if self.max_queue_size > 0 and self._queue.qsize() >= self.max_queue_size:
            raise asyncio.QueueFull(f"Task queue full (max: {self.max_queue_size})")
        
        task.status = TaskStatus.PENDING
        await self._queue.put(task)
        logger.debug(f"Task added: {task.name} (id={task.task_id}, priority={task.priority})")
        return task.task_id
    
    async def _worker(self, worker_id: int) -> None:
        """Worker coroutine that processes tasks from the queue."""
        logger.debug(f"Worker {worker_id} started")
        
        while self._running:
            try:
                # Get task with timeout to allow checking running flag
                try:
                    task = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                
                await self._execute_task(task, worker_id)
                self._queue.task_done()
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Worker {worker_id} error: {e}")
        
        logger.debug(f"Worker {worker_id} stopped")
    
    async def _execute_task(self, task: Task, worker_id: int) -> None:
        """Execute a single task with retry logic."""
        async with self._lock:
            self._active_tasks[task.task_id] = task
        
        task.status = TaskStatus.RUNNING
        task.started_at = datetime.utcnow()
        
        while task.retry_count <= task.max_retries:
            try:
                async with self._semaphore:
                    logger.info(f"Worker {worker_id} executing: {task.name}")
                    task.result = await task.func(*task.args, **task.kwargs)
                
                task.status = TaskStatus.COMPLETED
                task.completed_at = datetime.utcnow()
                logger.info(f"Task completed: {task.name} (id={task.task_id})")
                break
                
            except Exception as e:
                task.retry_count += 1
                task.error = str(e)
                
                if task.retry_count <= task.max_retries:
                    task.status = TaskStatus.RETRYING
                    logger.warning(
                        f"Task {task.name} failed (attempt {task.retry_count}/{task.max_retries + 1}): {e}"
                    )
                    await asyncio.sleep(task.retry_delay * task.retry_count)
                else:
                    task.status = TaskStatus.FAILED
                    task.completed_at = datetime.utcnow()
                    logger.error(f"Task failed permanently: {task.name} - {e}")
        
        async with self._lock:
            self._active_tasks.pop(task.task_id, None)
            self._completed_tasks[task.task_id] = task
    
    def get_task_status(self, task_id: str) -> Optional[Task]:
        """Get the status of a task by ID."""
        if task_id in self._active_tasks:
            return self._active_tasks[task_id]
        return self._completed_tasks.get(task_id)
    
    def get_queue_stats(self) -> Dict[str, Any]:
        """Get current queue statistics."""
        return {
            "running": self._running,
            "pending_tasks": self._queue.qsize(),
            "active_tasks": len(self._active_tasks),
            "completed_tasks": len(self._completed_tasks),
            "max_concurrent": self.max_concurrent,
            "max_queue_size": self.max_queue_size,
        }
    
    def background_task(
        self,
        name: Optional[str] = None,
        priority: TaskPriority = TaskPriority.NORMAL,
        max_retries: int = 3,
    ):
        """
        Decorator to automatically queue a function as a background task.
        
        Usage:
            @task_queue.background_task(priority=TaskPriority.LOW)
            async def my_func(arg1, arg2):
                ...
            
            # Call normally - it will be queued automatically
            await my_func(arg1, arg2)
        """
        def decorator(func: Callable[..., Coroutine[Any, Any, Any]]):
            async def wrapper(*args, **kwargs):
                task = Task(
                    name=name or func.__name__,
                    func=func,
                    args=args,
                    kwargs=kwargs,
                    priority=priority,
                    max_retries=max_retries,
                )
                return await self.add_task(task)
            return wrapper
        return decorator
    
    async def clear_completed(self, older_than_seconds: float = 3600) -> int:
        """
        Clear completed tasks older than specified age.
        
        Args:
            older_than_seconds: Clear tasks older than this many seconds
            
        Returns:
            Number of tasks cleared
        """
        cutoff = datetime.utcnow()
        cleared = 0
        
        async with self._lock:
            to_remove = []
            for task_id, task in self._completed_tasks.items():
                if task.completed_at and (cutoff - task.completed_at).total_seconds() > older_than_seconds:
                    to_remove.append(task_id)
            
            for task_id in to_remove:
                del self._completed_tasks[task_id]
                cleared += 1
        
        return cleared


# Global task queue instance
task_queue = TaskQueue(max_concurrent=2, max_queue_size=100)
