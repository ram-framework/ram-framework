import os
import sys
import fcntl
import select


def waitkey(message=None, timeout=0):

    def _press(waitfor):
        return select.select([sys.stdin], [], [], waitfor) == ([sys.stdin], [], [])

    def _drain():
        while _press(0):
            sys.stdin.read(1)

    def _write(s):
        sys.stdout.write(s)
        sys.stdout.flush()

    if message is None:
        message = "Press ENTER to continue"

    press = False

    try:
        _drain()
        _write(message)

        if timeout:
            _write(':')
        else:
            _write(' ... ')

        waitfor = 0
        while not timeout or waitfor < abs(timeout):
            press = _press(1)
            if press:
                break
            if timeout:
                _write('.')
                waitfor += 1
        else:
            raise OverflowError(timeout)
    finally:
        if not press:
            _write('\n')
        _drain()


# issues
#
#   scenario: service ntpd restart
#       sub-daemons inherite pipe descritors -- use fd_cloexec flag for pipe descriptors
#
#   scenario: sh -c 'read -p "Press ENTER to continue"'
#       no line endings in output -- use integer buffering with buffer size=1. by default line buffering is used
#
# limitations
#
#   captured output doesn't catch line end generated by user input
#   captured output doesn't block caller. re-output could appeared over snack forms

class Capture(object):
    def __init__(self, buffered=None, handlers=None):
        try:
            self.r, self.w = os.openpty()
        except OSError:
            self.r, self.w = os.pipe()
        self.od_stdout = sys.__stdout__.fileno()
        self.od_stderr = sys.__stderr__.fileno()

        self.buffered = buffered

        if handlers is None:
            handlers = []
        self.handlers = handlers

    def __capture(self, orig, swap):
        copy = os.dup(orig)

        flag = fcntl.fcntl(copy, fcntl.F_GETFL) | fcntl.FD_CLOEXEC
        fcntl.fcntl(copy, fcntl.F_SETFD, flag)

        os.dup2(swap, orig)

        return copy

    def __restore(self, orig, copy):
        os.dup2(copy, orig)
        os.close(copy)

    def __enter__(self):
        self.child_pid = os.fork()

        if self.child_pid:
            os.close(self.r)

            self.nd_stdout = self.__capture(self.od_stdout, self.w)
            self.nd_stderr = self.__capture(self.od_stderr, self.w)

            self.sys_stdout, sys.stdout = sys.stdout, os.fdopen(self.od_stdout, 'w', 0)
            self.sys_stderr, sys.stderr = sys.stderr, os.fdopen(self.od_stderr, 'w', 0)

            os.close(self.w)
        else:
            os.close(self.w)

            try:
                fobj = os.fdopen(self.r)

                if self.buffered:
                    while fobj:
                        data = fobj.read(self.buffered)
                        if not data:
                            fobj = None
                        else:
                            self(data)
                else:
                    for line in iter(fobj.readline, ''):
                        self(line)
            finally:
                os._exit(0)

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.child_pid:
            sys.stderr, self.sys_stderr = self.sys_stderr, None
            sys.stdout, self.sys_stdout = self.sys_stdout, None

            self.__restore(self.od_stderr, self.nd_stderr)
            self.__restore(self.od_stdout, self.nd_stdout)

            os.waitpid(self.child_pid, 0)
        else:
            raise RuntimeError("Shouldn't exit context in child!")

    def __call__(self, chars):
        def _stdout(chars):
            sys.stdout.write(chars)
            sys.stdout.flush()

        for handler in self.handlers + [_stdout]:
            if chars is None:
                break
            else:
                chars = handler(chars)


def capture(buffered=None, handlers=None):
    return Capture(buffered=buffered, handlers=handlers)


class FancyLine(object):
    def __init__(self, fancy1=None, fancy2=None):
        self.linebreak = True

        if fancy1 is None:
            self.fancy1 = ''
        else:
            self.fancy1 = fancy1

        if fancy2 is None:
            self.fancy2 = self.fancy1
        else:
            self.fancy2 = fancy2

    def __call__(self, chars):
        ready = ''
        for char in chars:
            if self.linebreak:
                ready += self.fancy1
                self.linebreak = False
            if char == '\n':
                ready += self.fancy2
                self.linebreak = True
            if char == '\r':
                continue
            ready += char
        return ready


class Bufferize(object):
    def __init__(self):
        self.r, self.w = os.pipe()

    def __call__(self, chars):
        for _char in chars:
            if select.select([], [self.w], [], 0) == ([], [self.w], []):
                os.write(self.w, _char)
            else:
                raise BufferError()
        return chars

    def __iter__(self):
        if select.select([self.r], [], [], 0) == ([self.r], [], []):
            return iter(os.read(self.r, select.PIPE_BUF).splitlines())
        else:
            return iter(())
