#! /usr/bin/env python

#  This program is free software: you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  The text of the license conditions can be read at
#  <http://www.gnu.org/licenses/>.
#
#  GitResultsManager by by Jason Yosinski <jason@yosinski.com>
#  Included asyncproc code by Thomas Bellman <bellman@lysator.liu.se>


import os
import sys
import logging
import stat
import subprocess
import datetime
import time
import errno
import signal
import threading
import pdb

__all__ = [ 'fmtSeconds', 'GitResultsManager', 'resman' ]



def fmtSeconds(sec):
    sign = ''
    if sec < 0:
        sign = '-'
        sec = -sec
    hours, remainder = divmod(sec, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours > 0:
        return sign + '%d:%02d:%02d' % (hours, minutes, int(seconds)) + ('%.3f' % (seconds-int(seconds)))[1:]
    elif minutes > 0:
        return sign + '%d:%02d' % (minutes, int(seconds)) + ('%.3f' % (seconds-int(seconds)))[1:]
    else:
        return sign + '%d' % int(seconds) + ('%.3f' % (seconds-int(seconds)))[1:]



class OutstreamHandler(object):
    def __init__(self, writeHandler, flushHandler):
        self.writeHandler = writeHandler
        self.flushHandler = flushHandler

    def write(self, message):
        self.writeHandler(message)

    def flush(self):
        self.flushHandler()



class OutputLogger(object):
    '''A logging utility to override sys.stdout'''

    '''Buffer states'''
    class BState:
        EMPTY  = 0
        STDOUT = 1
        STDERR = 2
            
    def __init__(self, filename):
        self.stdout = sys.stdout
        self.stderr = sys.stderr
        self.log = logging.getLogger('autologger')
        self.log.propagate = False
        self.log.setLevel(logging.DEBUG)
        self.fileHandler = logging.FileHandler(filename)
        formatter = logging.Formatter('%(asctime)s.%(msecs)03d %(message)s', datefmt='%y.%m.%d.%H.%M.%S')
        self.fileHandler.setFormatter(formatter)
        self.log.addHandler(self.fileHandler)

        self.stdOutHandler = OutstreamHandler(self.handleWriteOut,
                                              self.handleFlushOut)
        self.stdErrHandler = OutstreamHandler(self.handleWriteErr,
                                              self.handleFlushErr)
        self.buffer = ''
        self.bufferState = self.BState.EMPTY
        self.started = False


    def startCapture(self):
        if self.started:
            raise Exception('ERROR: OutputLogger capture was already started.')
        self.started = True
        sys.stdout = self.stdOutHandler
        sys.stderr = self.stdErrHandler

    def finishCapture(self):
        if not self.started:
            raise Exception('ERROR: OutputLogger capture was not started.')
        self.started = False
        self.flush()
        sys.stdout = self.stdout
        sys.stderr = self.stderr

    def handleWriteOut(self, message):
        self.write(message, self.BState.STDOUT)
        
    def handleWriteErr(self, message):
        self.write(message, self.BState.STDERR)

    def handleFlushOut(self):
        self.flush()
        
    def handleFlushErr(self):
        self.flush()
        
    def write(self, message, destination):
        if destination == self.BState.STDOUT:
            self.stdout.write(message)
        else:
            self.stderr.write(message)
        
        if destination == self.bufferState or self.bufferState == self.BState.EMPTY:
            self.buffer += message
            self.bufferState = destination
        else:
            # flush and change buffer
            self.flush()
            assert(self.buffer == '')
            self.bufferState = destination
            self.buffer = '' + message
        if '\n' in self.buffer:
            self.flush()

    def flush(self):
        self.stdout.flush()
        self.stderr.flush()
        if self.bufferState != self.BState.EMPTY:
            if len(self.buffer) > 0 and self.buffer[-1] == '\n':
                self.buffer = self.buffer[:-1]
            if self.bufferState == self.BState.STDOUT:
                for line in self.buffer.split('\n'):
                    self.log.info('  ' + line)
            elif self.bufferState == self.BState.STDERR:
                for line in self.buffer.split('\n'):
                    self.log.info('* ' + line)
            self.buffer = ''
            self.bufferState = self.BState.EMPTY
        self.fileHandler.flush()



def runCmd(args, supressErr = False):
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out,err = proc.communicate()
    code = proc.wait()

    if code != 0 and not supressErr:
        print out
        print err
        raise Exception('Got error from running command with args ' + repr(args))

    return code, out, err



def gitWorks():
    code,out,err = runCmd(('git','status'), supressErr = True)
    return code == 0



def gitLastCommit():
    return runCmd(('git', 'rev-parse', '--short', 'HEAD'))[1].strip()



def gitCurrentBranch():
    code, out, err = runCmd(('git', 'branch'))
    for line in out.split('\n'):
        if len(line) > 2 and line[0] == '*':
            return line[2:]
    raise Exception('Error getting current branch from git stdout/stderr %s, %s.' % (repr(out), repr(err)))



def gitStatus():
    return runCmd(('git', 'status'))[1].strip()



def gitDiff(color = False):
    if color:
        return runCmd(('git', 'diff', '--color'))[1].strip()
    else:
        return runCmd(('git', 'diff'))[1].strip()



def hostname():
    return runCmd('hostname')[1].strip()



def env():
    return runCmd('env')[1].strip()



RESULTS_SUBDIR = 'results'

class GitResultsManager(object):
    '''Creates directory for results. If created with
    resumeExistingRun, load info from that run, usually just so the
    run can be finished and the diary properly terminated.'''

    def __init__(self, resultsSubdir = None, resumeExistingRun = None):
        self._resumeExistingRun = resumeExistingRun
        if self._resumeExistingRun:
            # if user provided a directory to load in.
            try:
                dirExists = stat.S_ISDIR(os.stat(self._resumeExistingRun).st_mode)
            except OSError:
                pass
            if not dirExists:
                raise Exception('Tried to resume run from "%s", but it is not a results directory', self._resumeExistingRun)

            with open(os.path.join(self._resumeExistingRun, 'diary'), 'r') as diaryFile:
                firstLine = diaryFile.next()
            ints = [int(xx) for xx in firstLine.split()[0].split('.')]
            year,month,day,hour,minute,second,ms = ints

            startWallDt = datetime.datetime(year + 2000, month, day, hour, minute, second, ms * 1000)
            self.startWall = time.mktime(startWallDt.timetuple())
            self.startProc = None
            self.diary = False   # External run, so it's not a diary we're managing

            print 'grabbed time:', self.startWall

        else:
            self._resultsSubdir = resultsSubdir
            if self._resultsSubdir is None:
                self._resultsSubdir = RESULTS_SUBDIR
            self._name = None
            self._outLogger = None
            self.diary = None
        
    def start(self, description = '', diary = True):
        dirExists = False
        try:
            dirExists = stat.S_ISDIR(os.stat(self._resultsSubdir).st_mode)
        except OSError:
            pass
        if not dirExists:
            raise Exception('Please create the results directory "%s" first.' % self._resultsSubdir)

        if ' ' in description:
            raise Exception('Description must not contain any spaces, but it is "%s"' % description)

        if self._name is not None:
            self.finish()
        self.diary = diary

        # Test git
        useGit = gitWorks()

        timestamp = datetime.datetime.now().strftime('%y%m%d_%H%M%S')
        if useGit:
            lastCommit = gitLastCommit()
            curBranch = gitCurrentBranch()
            basename = '%s_%s_%s' % (timestamp, lastCommit, curBranch)
        else:
            basename = '%s' % timestamp

        if description:
            basename += '_%s' % description
        success = False
        ii = 0
        while not success:
            name = basename + ('_%d' % ii if ii > 0 else '')
            try:
                os.mkdir(os.path.join(self._resultsSubdir, name))
                success = True
            except OSError:
                print >>sys.stderr, name, 'already exists, appending suffix to name'
                ii += 1
        self._name = name

        if self.diary:
            self._outLogger = OutputLogger(os.path.join(self.rundir, 'diary'))
            self._outLogger.startCapture()

        self.startWall = time.time()
        self.startProc = time.clock()

        # TODO: remove redundancy
        # print the command that was executed
        print >>sys.stderr, 'WARNING: GitResultsManager running in GIT_DISABLED mode! (Is this a git repo?)'
        print '  Logging directory:', self.rundir
        print '        Command run:', ' '.join(sys.argv)
        print '           Hostname:', hostname()
        print '  Working directory:', os.getcwd()
        if not self.diary:
            print '<diary not saved>'
            # just log these three lines
            with open(os.path.join(self.rundir, 'diary'), 'w') as ff:
                print >>ff, 'WARNING: GitResultsManager running in GIT_DISABLED mode! (Is this a git repo?)'
                print >>ff, '  Logging directory:', self.rundir
                print >>ff, '        Command run:', ' '.join(sys.argv)
                print >>ff, '           Hostname:', hostname()
                print >>ff, '  Working directory:', os.getcwd()
                print >>ff, '<diary not saved>'

        if useGit:
            with open(os.path.join(self.rundir, 'gitinfo'), 'w') as ff:
                ff.write('%s %s\n' % (lastCommit, curBranch))
            with open(os.path.join(self.rundir, 'gitstat'), 'w') as ff:
                ff.write(gitStatus() + '\n')
            with open(os.path.join(self.rundir, 'gitdiff'), 'w') as ff:
                ff.write(gitDiff() + '\n')
            with open(os.path.join(self.rundir, 'gitcolordiff'), 'w') as ff:
                ff.write(gitDiff(color=True) + '\n')
        with open(os.path.join(self.rundir, 'env'), 'w') as ff:
            ff.write(env() + '\n')

    def stop(self):
        if self._resumeExistingRun:
            procTimeSec = '<unknown, not managed by GitResultsManager>'
        else:
            procTimeSec = fmtSeconds(time.clock() - self.startProc)
        if not self.diary:
            # just log these couple lines before resetting our name
            with open(os.path.join(self.rundir, 'diary'), 'a') as ff:
                print >>ff, '       Wall time: ', fmtSeconds(time.time() - self.startWall)
                print >>ff, '  Processor time: ', procTimeSec
        self._name = None
        print '       Wall time: ', fmtSeconds(time.time() - self.startWall)
        print '  Processor time: ', procTimeSec
        if self.diary:
            self._outLogger.finishCapture()
            self._outLogger = None


    @property
    def rundir(self):
        if self._resumeExistingRun:
            return self._resumeExistingRun
        elif self._name:
            return os.path.join(self._resultsSubdir, self._name)

    @property
    def runname(self):
        if self._resumeExistingRun:
            raise Exception('Not Implemented: Name not defined when runs are resumed.')
        return self._name



# Instantiate a global GitResultsManager for others to use
resman = GitResultsManager()



######################
# BEGIN asyncproc.py
######################

# asyncproc.py, modified from original version by Thomas Bellman <bellman@lysator.liu.se>
# URL: http://www.lysator.liu.se/~bellman/download
# Base version: asyncproc.py,v 1.9 2007/08/06 18:29:24 bellman Exp



class Timeout(Exception):
    """Exception raised by with_timeout() when the operation takes too long.
    """
    pass


def with_timeout(timeout, func, *args, **kwargs):
    """Call a function, allowing it only to take a certain amount of time.
       Parameters:
        - timeout        The time, in seconds, the function is allowed to spend.
                        This must be an integer, due to limitations in the
                        SIGALRM handling.
        - func                The function to call.
        - *args                Non-keyword arguments to pass to func.
        - **kwargs        Keyword arguments to pass to func.

       Upon successful completion, with_timeout() returns the return value
       from func.  If a timeout occurs, the Timeout exception will be raised.

       If an alarm is pending when with_timeout() is called, with_timeout()
       tries to restore that alarm as well as possible, and call the SIGALRM
       signal handler if it would have expired during the execution of func.
       This may cause that signal handler to be executed later than it would
       normally do.  In particular, calling with_timeout() from within a
       with_timeout() call with a shorter timeout, won't interrupt the inner
       call.  I.e.,
            with_timeout(5, with_timeout, 60, time.sleep, 120)
       won't interrupt the time.sleep() call until after 60 seconds.
    """

    class SigAlarm(Exception):
        """Internal exception used only within with_timeout().
        """
        pass

    def alarm_handler(signum, frame):
        raise SigAlarm()

    oldalarm = signal.alarm(0)
    oldhandler = signal.signal(signal.SIGALRM, alarm_handler)
    try:
        try:
            t0 = time.time()
            signal.alarm(timeout)
            retval = func(*args, **kwargs)
        except SigAlarm:
            raise Timeout("Function call took too long", func, timeout)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, oldhandler)
        if oldalarm != 0:
            t1 = time.time()
            remaining = oldalarm - int(t1 - t0 + 0.5)
            if remaining <= 0:
                # The old alarm has expired.
                os.kill(os.getpid(), signal.SIGALRM)
            else:
                signal.alarm(remaining)

    return retval



class Process(object):
    """Manager for an asynchronous process.
       The process will be run in the background, and its standard output
       and standard error will be collected asynchronously.

       Since the collection of output happens asynchronously (handled by
       threads), the process won't block even if it outputs large amounts
       of data and you do not call Process.read*().

       Similarly, it is possible to send data to the standard input of the
       process using the write() method, and the caller of write() won't
       block even if the process does not drain its input.

       On the other hand, this can consume large amounts of memory,
       potentially even exhausting all memory available.

       Parameters are identical to subprocess.Popen(), except that stdin,
       stdout and stderr default to subprocess.PIPE instead of to None.
       Note that if you set stdout or stderr to anything but PIPE, the
       Process object won't collect that output, and the read*() methods
       will always return empty strings.  Also, setting stdin to something
       other than PIPE will make the write() method raise an exception.
    """

    def __init__(self, *params, **kwparams):
        if len(params) <= 3:
            kwparams.setdefault('stdin', subprocess.PIPE)
        if len(params) <= 4:
            kwparams.setdefault('stdout', subprocess.PIPE)
        if len(params) <= 5:
            kwparams.setdefault('stderr', subprocess.PIPE)
        self._pending_input = []
        self._collected_outdata = []
        self._collected_errdata = []
        self._exitstatus = None
        self._lock = threading.Lock()
        self._inputsem = threading.Semaphore(0)
        # Flag telling feeder threads to quit
        self._quit = False

        self._process = subprocess.Popen(*params, **kwparams)

        if self._process.stdin:
            self._stdin_thread = threading.Thread(
                name="stdin-thread",
                target=self._feeder, args=(self._pending_input,
                                            self._process.stdin))
            self._stdin_thread.setDaemon(True)
            self._stdin_thread.start()
        if self._process.stdout:
            self._stdout_thread = threading.Thread(
                name="stdout-thread",
                target=self._reader, args=(self._collected_outdata,
                                            self._process.stdout))
            self._stdout_thread.setDaemon(True)
            self._stdout_thread.start()
        if self._process.stderr:
            self._stderr_thread = threading.Thread(
                name="stderr-thread",
                target=self._reader, args=(self._collected_errdata,
                                            self._process.stderr))
            self._stderr_thread.setDaemon(True)
            self._stderr_thread.start()

    def __del__(self, _killer=os.kill, _sigkill=signal.SIGKILL):
        if self._exitstatus is None:
            _killer(self.pid(), _sigkill)

    def pid(self):
        """Return the process id of the process.
           Note that if the process has died (and successfully been waited
           for), that process id may have been re-used by the operating
           system.
        """
        return self._process.pid

    def kill(self, signal):
        """Send a signal to the process.
           Raises OSError, with errno set to ECHILD, if the process is no
           longer running.
        """
        if self._exitstatus is not None:
            # Throwing ECHILD is perhaps not the most kosher thing to do...
            # ESRCH might be considered more proper.
            raise OSError(errno.ECHILD, os.strerror(errno.ECHILD))
        os.kill(self.pid(), signal)

    def wait(self, flags=0):
        """Return the process' termination status.

           If bitmask parameter 'flags' contains os.WNOHANG, wait() will
           return None if the process hasn't terminated.  Otherwise it
           will wait until the process dies.

           It is permitted to call wait() several times, even after it
           has succeeded; the Process instance will remember the exit
           status from the first successful call, and return that on
           subsequent calls.
        """
        if self._exitstatus is not None:
            return self._exitstatus
        pid,exitstatus = os.waitpid(self.pid(), flags)
        if pid == 0:
            return None
        if os.WIFEXITED(exitstatus) or os.WIFSIGNALED(exitstatus):
            self._exitstatus = exitstatus
            # If the process has stopped, we have to make sure to stop
            # our threads.  The reader threads will stop automatically
            # (assuming the process hasn't forked), but the feeder thread
            # must be signalled to stop.
            if self._process.stdin:
                self.closeinput()
            # We must wait for the reader threads to finish, so that we
            # can guarantee that all the output from the subprocess is
            # available to the .read*() methods.
            # And by the way, it is the responsibility of the reader threads
            # to close the pipes from the subprocess, not our.
            if self._process.stdout:
                self._stdout_thread.join()
            if self._process.stderr:
                self._stderr_thread.join()
        return exitstatus

    def terminate(self, graceperiod=1):
        """Terminate the process, with escalating force as needed.
           First try gently, but increase the force if it doesn't respond
           to persuassion.  The levels tried are, in order:
            - close the standard input of the process, so it gets an EOF.
            - send SIGTERM to the process.
            - send SIGKILL to the process.
           terminate() waits up to GRACEPERIOD seconds (default 1) before
           escalating the level of force.  As there are three levels, a total
           of (3-1)*GRACEPERIOD is allowed before the process is SIGKILL:ed.
           GRACEPERIOD must be an integer, and must be at least 1.
              If the process was started with stdin not set to PIPE, the
           first level (closing stdin) is skipped.
        """
        if self._process.stdin:
            # This is rather meaningless when stdin != PIPE.
            self.closeinput()
            try:
                return with_timeout(graceperiod, self.wait)
            except Timeout:
                pass

        self.kill(signal.SIGTERM)
        try:
            return with_timeout(graceperiod, self.wait)
        except Timeout:
            pass

        self.kill(signal.SIGKILL)
        return self.wait()

    def _reader(self, collector, source):
        """Read data from source until EOF, adding it to collector.
        """
        while True:
            data = os.read(source.fileno(), 65536)
            self._lock.acquire()
            collector.append(data)
            self._lock.release()
            if data == "":
                source.close()
                break
        return

    def _feeder(self, pending, drain):
        """Feed data from the list pending to the file drain.
        """
        while True:
            self._inputsem.acquire()
            self._lock.acquire()
            if not pending  and         self._quit:
                drain.close()
                self._lock.release()
                break
            data = pending.pop(0)
            self._lock.release()
            drain.write(data)

    def read(self):
        """Read data written by the process to its standard output.
        """
        self._lock.acquire()
        outdata = "".join(self._collected_outdata)
        del self._collected_outdata[:]
        self._lock.release()
        return outdata

    def readerr(self):
        """Read data written by the process to its standard error.
        """
        self._lock.acquire()
        errdata = "".join(self._collected_errdata)
        del self._collected_errdata[:]
        self._lock.release()
        return errdata

    def readboth(self):
        """Read data written by the process to its standard output and error.
           Return value is a two-tuple ( stdout-data, stderr-data ).

           WARNING!  The name of this method is ugly, and may change in
           future versions!
        """
        self._lock.acquire()
        outdata = "".join(self._collected_outdata)
        del self._collected_outdata[:]
        errdata = "".join(self._collected_errdata)
        del self._collected_errdata[:]
        self._lock.release()
        return outdata,errdata

    def _peek(self):
        self._lock.acquire()
        output = "".join(self._collected_outdata)
        error = "".join(self._collected_errdata)
        self._lock.release()
        return output,error

    def write(self, data):
        """Send data to a process's standard input.
        """
        if self._process.stdin is None:
            raise ValueError("Writing to process with stdin not a pipe")
        self._lock.acquire()
        self._pending_input.append(data)
        self._inputsem.release()
        self._lock.release()

    def closeinput(self):
        """Close the standard input of a process, so it receives EOF.
        """
        self._lock.acquire()
        self._quit = True
        self._inputsem.release()
        self._lock.release()


class ProcessManager(object):
    """Manager for asynchronous processes.
       This class is intended for use in a server that wants to expose the
       asyncproc.Process API to clients.  Within a single process, it is
       usually better to just keep track of the Process objects directly
       instead of hiding them behind this.  It probably shouldn't have been
       made part of the asyncproc module in the first place.
    """

    def __init__(self):
        self.__last_id = 0
        self.__procs = {}

    def start(self, args, executable=None, shell=False, cwd=None, env=None):
        """Start a program in the background, collecting its output.
           Returns an integer identifying the process.        (Note that this
           integer is *not* the OS process id of the actuall running
           process.)
        """
        proc = Process(args=args, executable=executable, shell=shell,
                       cwd=cwd, env=env)
        self.__last_id += 1
        self.__procs[self.__last_id] = proc
        return self.__last_id

    def kill(self, procid, signal):
        return self.__procs[procid].kill(signal)

    def terminate(self, procid, graceperiod=1):
        return self.__procs[procid].terminate(graceperiod)

    def write(self, procid, data):
        return self.__procs[procid].write(data)

    def closeinput(self, procid):
        return self.__procs[procid].closeinput()

    def read(self, procid):
        return self.__procs[procid].read()

    def readerr(self, procid):
        return self.__procs[procid].readerr()

    def readboth(self, procid):
        return self.__procs[procid].readboth()

    def wait(self, procid, flags=0):
        """
           Unlike the os.wait() function, the process will be available
           even after ProcessManager.wait() has returned successfully,
           in order for the process' output to be retrieved.  Use the
           reap() method for removing dead processes.
        """
        return self.__procs[procid].wait(flags)

    def reap(self, procid):
        """Remove a process.
           If the process is still running, it is killed with no pardon.
           The process will become unaccessible, and its identifier may
           be reused immediately.
        """
        if self.wait(procid, os.WNOHANG) is None:
            self.kill(procid, signal.SIGKILL)
        self.wait(procid)
        del self.__procs[procid]

    def reapall(self):
        """Remove all processes.
           Running processes are killed without pardon.
        """
        # Since reap() modifies __procs, we have to iterate over a copy
        # of the keys in it.  Thus, do not remove the .keys() call.
        for procid in self.__procs.keys():
            self.reap(procid)

######################
# END asyncproc.py
######################



if __name__ == '__main__':
    print 'This is just a simple demo. See the examples directory in the GitResultsManager distribution for more detailed examples.'

    resman.start()
    print 'this is being logged to the %s directory' % resman.rundir
    time.sleep(1)
    print 'this is being logged to the %s directory' % resman.rundir
    time.sleep(1)
    print 'this is being logged to the %s directory' % resman.rundir
    resman.stop()