`Scheduler` in `scheduler.py` is a priority queue of tasks. Reported problems: adding two tasks with the same priority raises `TypeError` when the tasks are dicts; tasks of equal priority come out in an arbitrary order; and `remove(task)` removes every equal task and sometimes leaves the queue returning tasks in the wrong order.

Fix it so that:

- `add(priority, task)` queues any object as a task (tasks are never compared). `pop()` returns the task with the **lowest** priority number; tasks with equal priority come out in the order they were added (FIFO). `pop()` on an empty scheduler raises `IndexError`.
- `peek()` returns the task `pop()` would return without removing it (`IndexError` when empty).
- `remove(task)` cancels the **earliest-added** pending task that is equal to `task` and returns `True`, or returns `False` if there is none. Ordering of the others is unaffected.
- `len(scheduler)` is the number of pending tasks.
