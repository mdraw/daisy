from __future__ import absolute_import

from .dependency_graph import DependencyGraph
from .task import Task
from .block import BlockStatus

from typing import List
import collections
import logging

logger = logging.getLogger(__name__)

# used for automated testing to speed up tests as the status thread takes a
# long time to shutdown
_NO_SPAWN_STATUS_THREAD = False


class TaskState:
    def __init__(self):
        self.started = False
        self.ready_count = 0
        self.total_block_count = 0
        self.completed_count = 0
        self.failed_count = 0
        self.orphaned_count = 0
        self.processing_count = 0

    def __str__(self):
        return f"{self.started}, {self.ready_count}"

    def __repr__(self):
        return str(self)


class TaskBlocks:
    def __init__(self):
        self.ready_queue = collections.deque()
        self.processing_blocks = set()
        self.failed_blocks = set()
        self.orphaned_blocks = set()
        self.completed_blocks = set()


class Scheduler:
    """This is the main scheduler that tracks states of tasks.

    The Scheduler takes a list of tasks, and upon request will
    provide the next block available for processing.

    Usage:

    .. code:: python

        graph = DependencyGraph(...)
        return Scheduler().distribute(graph)

    See the DependencyGraph class for more information.
    """

    def __init__(self, tasks: List[Task]):
        self.dependency_graph = DependencyGraph(tasks)

        self.task_map = {}
        self.task_states = collections.defaultdict(TaskState)
        self.task_blocks = collections.defaultdict(TaskBlocks)

        self.root_tasks = self.dependency_graph.roots()

        for task in tasks:
            self.__init_task(task)

        self.completed_surface = set()
        self.block_statuses = collections.defaultdict(BlockStatus)

        self.last_prechecked = collections.defaultdict(lambda: (None, None))

    def __init_task(self, task):
        if task.task_id not in self.task_map:
            self.task_map[task.task_id] = task
            self.task_states[
                task.task_id
            ].total_block_count = self.dependency_graph.num_blocks(task.task_id)

            if task.task_id in self.root_tasks:
                self.task_states[task.task_id].ready_count = self.root_tasks[
                    task.task_id
                ][0]

            for upstream_task in task.requires():
                self.add(upstream_task)

    def has_next(self, task_id):
        has_ready_block = len(self.task_blocks[task_id].ready_queue) > 0
        if has_ready_block:
            return True
        elif self.task_states[task_id].ready_count > 0:
            # a task should only have ready blocks that are not in the queue
            # if it is a root task and roots have been provided as a generator
            try:
                next_block = next(self.root_tasks[task_id][1])
                self.task_blocks[task_id].ready_queue.append(next_block)
                return True
            except StopIteration:
                raise NotImplementedError("This should not be reachable!")
        else:
            # There are no blocks ready for processing
            return False

    def acquire_block(self, task_id):
        """
        Get a block that is ready to process for task with given task_id.

        Args:
            task_id(``int``):
                The task for which you want a block

        Return:
            ``Block`` or None:
                A block that can be run without worry of
                conflicts.
            ``TaskState``:
                The state of the task.
        """
        if self.has_next(task_id):
            block = self.task_blocks[task_id].ready_queue.popleft()
            pre_check_ret = self.precheck(task_id, block)

            if pre_check_ret:
                logger.debug(
                    "Skipping task %s block %d; already processed.",
                    task_id,
                    block.block_id,
                )
                block.status = BlockStatus.SUCCESS
                self.release_block(block)
                return self.acquire_block(task_id)

            else:
                self.task_states[task_id].started = (
                    self.task_states[task_id].started or True
                )
                self.task_blocks[task_id].processing_blocks.add(block.block_id)
                self.task_states[task_id].processing_count += 1
                return block

        else:
            return None

    def release_block(self, block):
        """
        Update the dependency graph with the status
        of a given block ``block`` on task ``task``.

        Args:
            task(``Task``):
                Task of interest.

            block(``Block``):
                Block of interest.

        Return:
            ``dict``(``task_id`` -> ``TaskStatus``):
            Each task returned had its
            state changed by updating the status of the given
            block on the given task. i.e. if a task B was
            dependent on task A, and marking a block in A
            as solved made some blocks in B available for
            processing, task B would update its state from
            waiting to Waiting to Ready and be returned.
        """
        task_id = block.task_id
        if block.status == BlockStatus.SUCCESS:
            self.task_blocks[task_id].processing_blocks.remove(block.block_id)
            self.task_blocks[task_id].completed_blocks.add(block.block_id)
            self.task_states[task_id].processing_count -= 1
            self.task_states[task_id].completed_count += 1
            self.update_complete_surface(block.block_id)
            updated_tasks = self.update_ready_queue(block.block_id)
            return updated_tasks
        if block.status == BlockStatus.FAILED:
            self.task_blocks[task_id].processing_blocks.remove(block.block_id)
            self.task_blocks[task_id].failed_blocks.add(block.block_id)
            self.task_states[task_id].processing_count -= 1
            self.task_states[task_id].failed_count += 1
            self.update_failed_surface(block.block_id)
            # No new blocks can be freed by failing a block.
            return {}

    def update_complete_surface(self, block_id):
        upstream_blocks = self.dependency_graph.upstream(block_id)
        self.completed_surface.add(block_id)
        for upstream_block in upstream_blocks:
            if upstream_block not in self.completed_surface:
                continue
            else:
                downstream_blocks = self.dependency_graph.downstream(upstream_block)
                if all(down in self.completed_surface for down in downstream_blocks):
                    self.completed_surface.remove(upstream_block)

    def update_ready_queue(self, block_id):
        updated_tasks = {}
        downstream_blocks = self.dependency_graph.downstream(block_id)
        for down in downstream_blocks:
            upstream_blocks = self.dependency_graph.upstream(down.block_id)
            if all(up.block_id in self.completed_surface for up in upstream_blocks):
                self.task_blocks[down.task_id].ready_queue.append(down)
                updated_tasks[down.task_id] = self.task_states[down.task_id]
        return updated_tasks

    def update_failed_surface(self, block_id):
        self.task_blocks[block_id[0]].failed_blocks.append(block_id)

        downstream = set(self.dependency_graph.downstream(block_id))
        while len(downstream) > 0:
            orphan = downstream.pop()
            self.task_blocks[orphan.task_id].add(orphan.block_id)
            self.task_states[orphan.task_id].orphaned_count += 1
            downstream = downstream.union(set(self.dependency_graph.downstream(orphan)))

    def precheck(self, task_id, block):
        if self.last_prechecked[task_id][0] != block:
            # pre-check and skip blocks if possible
            try:
                # pre_check can intermittently fail
                # so we wrap it in a try block
                pre_check_ret = self.tasks[task_id]._daisy.pre_check(block)
            except Exception as e:
                logger.error(
                    "pre_check() exception for block %s of task %s. " "Exception: %s",
                    block,
                    task_id,
                    e,
                )
                pre_check_ret = False
            finally:
                self.last_prechecked[task_id] = (block, pre_check_ret)

        return self.last_prechecked[task_id][1]


def run_blockwise():
    raise NotImplementedError()


def distribute():
    raise NotImplementedError()
