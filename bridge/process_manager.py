import os
import subprocess
import threading
import time


class ProcessWrapper:
    def __init__(self, name, proc, stdout_thread, stderr_thread):
        self.name = name
        self.proc = proc
        self.stdout_thread = stdout_thread
        self.stderr_thread = stderr_thread
    def poll(self):
        try:
            return self.proc.poll()
        except Exception:
            return None


class ProcessManager:
    def __init__(self, logger):
        self.logger = logger
        self._procs = {}
        self._lock = threading.Lock()

    def _drain_pipe(self, pipe, level):
        try:
            for line in iter(pipe.readline, b""):
                try:
                    self.logger.log(level, line.decode(errors='ignore').rstrip())
                except Exception:
                    pass
        except Exception:
            pass

    def start(self, name, cmd, cwd=None):
        with self._lock:
            # Stop existing with same name
            w = self._procs.get(name)
            if w and getattr(w, 'proc', None) and w.proc.poll() is None:
                self.logger.info(f"ProcessManager: stopping existing process '{name}' before start")
                self.stop(name)

            self.logger.info(f"ProcessManager: starting '{name}': {' '.join(cmd)}")
            proc = subprocess.Popen(
                cmd,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )

            stdout_thread = threading.Thread(target=self._drain_pipe, args=(proc.stdout, 20), daemon=True)
            stderr_thread = threading.Thread(target=self._drain_pipe, args=(proc.stderr, 40), daemon=True)
            stdout_thread.start()
            stderr_thread.start()

            self._procs[name] = ProcessWrapper(name, proc, stdout_thread, stderr_thread)
            return self._procs[name]

    def stop(self, name, timeout=3):
        with self._lock:
            w = self._procs.get(name)
            if not w or w.proc is None:
                return
            proc = w.proc
            try:
                pgid = os.getpgid(proc.pid)
                self.logger.info(f"ProcessManager: terminating pgid={pgid} for '{name}'")
                try:
                    os.killpg(pgid, 15)
                except Exception:
                    pass
                # wait with timeout
                start = time.time()
                while proc.poll() is None and (time.time() - start) < timeout:
                    time.sleep(0.1)
                if proc.poll() is None:
                    try:
                        os.killpg(pgid, 9)
                    except Exception:
                        pass
            except Exception:
                try:
                    proc.terminate()
                except Exception:
                    pass
            finally:
                try:
                    if w.proc.stdout:
                        w.proc.stdout.close()
                    if w.proc.stderr:
                        w.proc.stderr.close()
                except Exception:
                    pass
                self._procs[name] = None

    def get(self, name):
        with self._lock:
            return self._procs.get(name)
