import os
import time
import select

from ram.process import running_py
from ram.process import running_ps
from ram.process import output
from ram.process import _quote_cmd
from ram.osutils import match_name


class WatchTimeoutError(Exception):
    pass


class Watch(object):
    def __init__(self, iopipe):
        self.iopipe = iopipe

    def fileno(self):
        return self.iopipe.fileno()

    def __nonzero__(self):
        return self.status()

    def __iter__(self):
        while self:
            try:
                data = self(0)
            except WatchTimeoutError:
                break
            else:
                yield data

    def select(self, timeout=None):
        return any(select.select([self.fileno()], [], [], timeout))

    def status(self):
        raise NotImplementedError()

    def update(self):
        raise NotImplementedError()

    def __call__(self, timeout=None, iterate=True):
        ready = self.select(timeout)

        if not ready:
            raise WatchTimeoutError("watch timed out.")
        elif iterate:
            return self.update()
        else:
            return None


class PipeWatch(Watch):
    def __init__(self, iopipe):
        super(PipeWatch, self).__init__(iopipe)
        self.eofile = False

    def status(self):
        return not self.eofile

    def update(self):
        data = ''
        while self.select(0):
            try:
                buff = os.read((self.fileno()), 4096)
            except OSError:
                buff = ''

            if buff:
                data += buff
            else:
                self.eofile = True
                break

        return data


class ExitWatch(Watch):
    def __init__(self, ioproc):
        self.ioproc = ioproc

    def fileno(self):
        return self.ioproc.stdin.fileno()

    def status(self):
        return self.ioproc.poll() is None

    def select(self, timeout=None):
        if timeout is None:
            return self.ioproc.wait() is not None
        else:
            ready = super(ExitWatch, self).select(timeout)
            while ready and self:
                time.sleep(0.001)
            return ready

    def update(self):
        return self.ioproc.poll()


def watch_status(command, *args):
    return running_ps(command, *args, wait=False, wrap=ExitWatch)


def watch_stdout(command, *args):
    return running_ps(command, *args, wait=False, wrap=lambda p: PipeWatch(p.stdout))


def watch_stderr(command, *args):
    return running_ps(command, *args, wait=False, wrap=lambda p: PipeWatch(p.stderr))


class IterWatch(Watch):
    def __init__(self, iopipe):
        super(IterWatch, self).__init__(iopipe)
        self.eofile = False

    def status(self):
        return not self.eofile

    def update(self):
        try:
            obj, _tb = self.iopipe.recv()
        except EOFError:
            self.eofile = True
            return None

        if _tb:
            _et = type(obj)
            raise _et("Unhandled exception in sub-process\n\n" + _tb.strip())
        else:
            return obj


def watch_iterable(iterable, name=None):
    def _wrap_iter(stdout=None):
        try:
            for obj in iterable:
                stdout.send((obj, None))
        except BaseException as exc:
            from traceback import format_exc
            _tb = "Process: %s\n%s" % (name, format_exc())

            stdout.send((exc, _tb))

    return running_py(_wrap_iter, wrap=lambda p: IterWatch(p.stdout))


def track_output(command, *args, **kwargs):
    timeout = kwargs.pop('timeout', None)
    _was_output = None
    for _ in track_timer(timeout):
        _now_output = output(command, *args, **kwargs)
        if _now_output != _was_output:
            _was_output = _now_output
            yield _now_output


def watch_output(command, *args, **kwargs):
    kwargs.setdefault('timeout', None)
    return watch_iterable(
        track_output(command, *args, **kwargs),
        name='watch: %s' % _quote_cmd(command, *args)
    )


def track_timer(timeout=None):
    if timeout is None:
        timeout = 1.0
    prev_time = time.time()
    while True:
        yield time.time()
        next_time = prev_time + timeout
        sleep_for = next_time - time.time()
        if sleep_for > 0.0:
            time.sleep(sleep_for % timeout)
            prev_time = next_time
        else:
            prev_time = next_time - sleep_for


def watch_timer(timeout=None):
    return watch_iterable(
        track_timer(timeout),
        name='timer'
    )


def track_dir(dirname, match=None, files=True, dirs=False, rec=False):
    import pyinotify

    class InotifyProcessEventQueue(pyinotify.ProcessEvent):
        def __init__(self):
            self.queue = []

        def __iter__(self):
            while self.queue:
                yield self.queue.pop(0)

        def process_IN_CREATE(self, event):
            self.queue.append(event)

        def process_IN_DELETE(self, event):
            self.queue.append(event)

        def process_IN_MOVED(self, event):
            self.queue.append(event)

    wm = pyinotify.WatchManager()
    mask = (
        pyinotify.IN_DELETE |
        pyinotify.IN_CREATE |
        pyinotify.IN_MOVED_FROM |
        pyinotify.IN_MOVED_TO
    )

    queue = InotifyProcessEventQueue()

    notifier = pyinotify.Notifier(wm, queue)
    wd = wm.add_watch(dirname, mask, rec=rec, auto_add=rec)

    while True:
        notifier.process_events()

        for event in queue:
            if not dirs and event.mask & pyinotify.IN_ISDIR:
                continue
            if not files and not event.mask & pyinotify.IN_ISDIR:
                continue
            if not match_name(event.name, match):
                continue

            is_deleted = bool(event.mask & (
                pyinotify.IN_DELETE |
                pyinotify.IN_MOVED_FROM
            ))

            is_created = bool(event.mask & (
                pyinotify.IN_CREATE |
                pyinotify.IN_MOVED_TO
            ))

            is_present = is_created if is_created != is_deleted else None

            yield (
                event.path,
                event.name,
                event.dir,
                is_present,
            )

        if notifier.check_events():
            notifier.read_events()


def watch_dir(dirname, match=None, files=True, dirs=False, rec=False):
    return watch_iterable(
        track_dir(dirname, match, files, dirs, rec),
        name='dir'
    )
