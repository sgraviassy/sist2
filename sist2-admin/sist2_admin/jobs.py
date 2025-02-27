import json
import logging
import os.path
import shutil
import signal
import uuid
from datetime import datetime
from enum import Enum
from hashlib import md5
from logging import FileHandler
from threading import Lock, Thread
from time import sleep
from uuid import uuid4, UUID

from hexlib.db import PersistentState
from pydantic import BaseModel, validator

from config import logger, LOG_FOLDER
from notifications import Notifications
from sist2 import ScanOptions, IndexOptions, Sist2, Sist2Index
from state import RUNNING_FRONTENDS
from web import Sist2Frontend


class JobStatus(Enum):
    CREATED = "created"
    STARTED = "started"
    INDEXED = "indexed"
    FAILED = "failed"


class Sist2Job(BaseModel):
    name: str
    scan_options: ScanOptions
    index_options: IndexOptions

    cron_expression: str
    schedule_enabled: bool = False

    previous_index: str = None
    last_index: str = None
    last_index_date: datetime = None
    status: JobStatus = JobStatus("created")
    last_modified: datetime
    etag: str = None
    do_full_scan: bool = False

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    @staticmethod
    def create_default(name: str):
        return Sist2Job(
            name=name,
            scan_options=ScanOptions(path="/"),
            index_options=IndexOptions(),
            last_modified=datetime.now(),
            cron_expression="0 0 * * *"
        )

    # @validator("etag", always=True)
    # def validate_etag(cls, value, values):
    #     s = values["name"] + values["scan_options"].json() + values["index_options"].json() + values["cron_expression"]
    #     return md5(s.encode()).hexdigest()


class Sist2TaskProgress:

    def __init__(self, done: int = 0, count: int = 0, index_size: int = 0, tn_size: int = 0, waiting: bool = False):
        self.done = done
        self.count = count
        self.index_size = index_size
        self.store_size = tn_size
        self.waiting = waiting

    def percent(self):
        return (self.done / self.count) if self.count else 0


class Sist2Task:

    def __init__(self, job: Sist2Job, display_name: str, depends_on: uuid.UUID = None):
        self.job = job
        self.display_name = display_name

        self.progress = Sist2TaskProgress()
        self.id = uuid4()
        self.pid = None
        self.started = None
        self.ended = None
        self.depends_on = depends_on

        self._logger = logging.Logger(name=f"{self.id}")
        self._logger.addHandler(FileHandler(os.path.join(LOG_FOLDER, f"sist2-{self.id}.log")))

    def json(self):
        return {
            "id": self.id,
            "job": self.job,
            "display_name": self.display_name,
            "progress": self.progress,
            "started": self.started,
            "ended": self.ended,
            "depends_on": self.depends_on,
        }

    def log_callback(self, log_json):

        if "progress" in log_json:
            self.progress = Sist2TaskProgress(**log_json["progress"])
        elif self._logger:
            self._logger.info(json.dumps(log_json))

    def run(self, sist2: Sist2, db: PersistentState):
        self.started = datetime.now()

        logger.info(f"Started task {self.display_name}")


class Sist2ScanTask(Sist2Task):

    def run(self, sist2: Sist2, db: PersistentState):
        super().run(sist2, db)

        self.job.scan_options.name = self.job.name

        if self.job.last_index and os.path.exists(self.job.last_index) and not self.job.do_full_scan:
            self.job.scan_options.incremental = self.job.last_index
        else:
            self.job.scan_options.incremental = None

        def set_pid(pid):
            self.pid = pid

        return_code = sist2.scan(self.job.scan_options, logs_cb=self.log_callback, set_pid_cb=set_pid)
        self.ended = datetime.now()

        if return_code != 0:
            self._logger.error(json.dumps({"sist2-admin": f"Process returned non-zero exit code ({return_code})"}))
            logger.info(f"Task {self.display_name} failed ({return_code})")
        else:
            index = Sist2Index(self.job.scan_options.output)

            # Save latest index
            self.job.previous_index = self.job.last_index

            self.job.last_index = index.path
            self.job.last_index_date = datetime.now()
            self.job.do_full_scan = False
            db["jobs"][self.job.name] = self.job
            self._logger.info(json.dumps({"sist2-admin": f"Save last_index={self.job.last_index}"}))

        logger.info(f"Completed {self.display_name} ({return_code=})")

        return return_code


class Sist2IndexTask(Sist2Task):

    def __init__(self, job: Sist2Job, display_name: str, depends_on: Sist2Task):
        super().__init__(job, display_name, depends_on=depends_on.id)

    def run(self, sist2: Sist2, db: PersistentState):
        super().run(sist2, db)

        self.job.index_options.path = self.job.scan_options.output

        return_code = sist2.index(self.job.index_options, logs_cb=self.log_callback)
        self.ended = datetime.now()

        duration = self.ended - self.started

        ok = return_code == 0

        if ok:
            # Remove old index
            if self.job.previous_index is not None:
                self._logger.info(json.dumps({"sist2-admin": f"Remove {self.job.previous_index=}"}))
                try:
                    shutil.rmtree(self.job.previous_index)
                except FileNotFoundError:
                    pass

            self.restart_running_frontends(db, sist2)

        # Update status
        self.job.status = JobStatus("indexed") if ok else JobStatus("failed")
        db["jobs"][self.job.name] = self.job

        self._logger.info(json.dumps({"sist2-admin": f"Sist2Scan task finished {return_code=}, {duration=}"}))

        logger.info(f"Completed {self.display_name} ({return_code=})")

        return return_code

    def restart_running_frontends(self, db: PersistentState, sist2: Sist2):
        for frontend_name, pid in RUNNING_FRONTENDS.items():
            frontend = db["frontends"][frontend_name]
            frontend: Sist2Frontend

            os.kill(pid, signal.SIGTERM)
            try:
                os.wait()
            except ChildProcessError:
                pass

            frontend.web_options.indices = map(lambda j: db["jobs"][j].last_index, frontend.jobs)

            pid = sist2.web(frontend.web_options, frontend.name)
            RUNNING_FRONTENDS[frontend_name] = pid

            self._logger.info(json.dumps({"sist2-admin": f"Restart frontend {pid=} {frontend_name=}"}))


class TaskQueue:
    def __init__(self, sist2: Sist2, db: PersistentState, notifications: Notifications):
        self._lock = Lock()

        self._sist2 = sist2
        self._db = db
        self._notifications = notifications

        self._tasks = {}
        self._queue = []
        self._sem = 0

        self._thread = Thread(target=self._check_new_task, daemon=True)
        self._thread.start()

    def _tasks_failed(self):
        done = set()

        for row in self._db["task_done"].sql("WHERE return_code != 0"):
            done.add(uuid.UUID(row["id"]))

        return done

    def _tasks_done(self):

        done = set()

        for row in self._db["task_done"]:
            done.add(uuid.UUID(row["id"]))

        return done

    def _check_new_task(self):
        while True:
            with self._lock:
                for task in list(self._queue):
                    task: Sist2Task

                    if self._sem >= 1:
                        break

                    if not task.depends_on or task.depends_on in self._tasks_done():
                        self._queue.remove(task)

                        if task.depends_on in self._tasks_failed():
                            # The task which we depend on failed, continue
                            continue

                        self._sem += 1

                        t = Thread(target=self._run_task, args=(task,))

                        self._tasks[task.id] = {
                            "task": task,
                            "thread": t,
                        }

                        t.start()
                    break
            sleep(1)

    def tasks(self):
        return list(map(lambda t: t["task"], self._tasks.values()))

    def kill_task(self, task_id):

        task = self._tasks.get(UUID(task_id))

        if task:
            pid = task["task"].pid
            logger.info(f"Killing task {task_id} (pid={pid})")
            os.kill(pid, signal.SIGTERM)
            return True

        return False

    def _run_task(self, task: Sist2Task):
        task_result = task.run(self._sist2, self._db)

        with self._lock:
            del self._tasks[task.id]
            self._sem -= 1

            self._db["task_done"][task.id] = {
                "ended": task.ended,
                "started": task.started,
                "name": task.display_name,
                "return_code": task_result
            }
        if isinstance(task, Sist2IndexTask):
            self._notifications.notify({
                "message": "notifications.indexCompleted",
                "job": task.job.name
            })

    def submit(self, task: Sist2Task):

        logger.info(f"Submitted task to queue {task.display_name}")

        with self._lock:
            self._queue.append(task)
