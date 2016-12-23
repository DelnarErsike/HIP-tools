#!/usr/bin/python3
# -*- python-indent-offset: 4 -*-

VERSION='0.02'

import os
import sys
import pwd
import time
import signal
import logging
import subprocess
import daemon
import lockfile
from pathlib import Path

g_daemon_user = 'hiphub'  # user hiphub should run as (will use user's default group)
g_base_dir = Path('/home') / g_daemon_user  # daemon state will be kept under here
g_webhook_dir = Path('/var/www/hip.zijistark.com/hiphub')  # folder where the webhook hints us as to new commit activity
g_root_repo_dir = Path('/var/local/git')  # root folder of all the hiphub git repositories
g_gitbin_path = Path('/usr/bin/git')

# repos and respective branches which we track
g_repos = {
    'SWMH-BETA': ['master', 'Timeline_Extension+Bookmark_BETA'],
    'sed2': ['dev', 'timeline'],
    'EMF': ['alpha', 'timeline'],
    'MiniSWMH': ['master', 'timeline'],
    'HIP-tools': ['master'],
    'ck2utils': ['dev'],
    'CPRplus': ['dev'],
    'ARKOpack': ['master'],
    'ArumbaKS': ['master'],
    'LTM': ['master', 'dev'],
}

g_pidfile_path = g_base_dir / 'hiphub.pid'  # we mark our currently running PID here
g_state_dir = g_base_dir / 'state'  # we store the last-processed SHA for each tracked head within files in this folder
g_logfile_path = g_base_dir / 'hiphub.log'  # we log info and errors here (once the daemon is off the ground and no longer attached to a session/terminal)


def fatal(msg):
    sys.stderr.write('fatal: ' + msg + '\n')
    sys.stderr.flush()
    sys.exit(1)


def shutdown_daemon():
    g_pidfile_path.unlink()
    logging.info('hiphub v{} shutting down with pid {}...'.format(VERSION, os.getpid()))
    sys.exit(0)

    
class TooManyRetriesException(Exception):
    def __str__(self):
        return 'Retried command too many times'


def git_run(args, retry=False):
    cmd = [str(g_gitbin_path)] + args
    n_tries = 0
    
    while True:
        cp = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        n_tries += 1

        if cp.returncode == 0:
            return cp
        else:
            logging.error('git failed:\n>command: {}\n>code: {}\n>error:\n{}\n'.format(cp.args, cp.returncode, cp.stderr))

            if not retry:
                cp.check_returncode()  # will throw for us
            elif n_tries == 1000:
                raise TooManyRetriesException()
            
            # TODO: finish this once we know a) what git will typically give us on stderr vs. stdout,
            # and b) what the errors for transient network issues tend to look like, as we only want to
            # to do "infinite" retry for those. [until then, setup errors and the like are just going to
            # keep printing a lot of failures to the log for a very long time if retries were enabled.]
            time.sleep(60)


def update_head(repo, branch):
    os.chdir(str(g_root_repo_dir / repo))

    # first, make the repo a carbon copy of HEAD -- remove changes to index, remove untracked changes
    git_run(['reset', '--hard', 'HEAD'])
    git_run(['clean', '-f'])

    # now checkout the desired branch and pull 
    git_run(['checkout', branch])
    git_run(['pull'], retry=True)

    # determine the head's possibly-new SHA
    cp = git_run(['rev-parse', 'HEAD'])
    
    os.chdir(str(g_base_dir))
    return cp.stdout.strip()

    
def load_state():
    global g_last_rev
    g_last_rev = {}
    for r in g_repos:
        for b in g_repos[r]:
            h = '{}:{}'.format(r, b)
            p = g_state_dir / h
            if p.exists():
                with p.open() as f:
                    g_last_rev[h] = f.read().strip()


def process_head_change(repo, branch, head_rev):
    # TODO: actual [selective] processing :)

    # update memory state
    h = '{}:{}'.format(repo, branch)
    g_last_rev[h] = head_rev

    # update state on disk
    with (g_state_dir / h).open("w") as f:
        print(head_rev, file=f)


def init_daemon():
    # mark our tracks with pidfile
    with g_pidfile_path.open("w") as f:
        print(os.getpid(), file=f)

    # created needed directories on-demand
    if not g_state_dir.exists():
        logging.debug('state folder does not exist, creating: {}'.format(g_state_dir))
        g_state_dir.mkdir(parents=True)

    load_state()

    logging.debug('updating all tracked heads...')
    proc_needed = []
    
    for repo in g_repos:
        logging.debug('refreshing repository {}...'.format(repo))
        for branch in g_repos[repo]:
            rev = update_head(repo, branch)
            h = '{}:{}'.format(repo, branch)
            if h not in g_last_rev or rev != g_last_rev[h]:
                proc_needed.append( (repo, branch, rev) )

    # TODO: might want to sort proc_needed so that, e.g., SWMH-BETA < MiniSWMH < EMF
    
    for pn in proc_needed:
        process_head_change(*pn)


def run_daemon():
    last_mtime = {}

    # initialize file mtimes
    for r in g_repos:
        for b in g_repos[r]:
            h = '{}:{}'.format(r, b)
            p = g_webhook_dir / h
            if p.exists():
                last_mtime[h] = p.stat().st_mtime
    
    while True:
        time.sleep(1)  # one-second polling interval

        changed_heads = []
        for r in g_repos:
            for b in g_repos[r]:
                h = '{}:{}'.format(r, b)
                p = g_webhook_dir / h
                if p.exists() and (h not in last_mtime or p.stat().st_mtime > last_mtime[h]):
                    logging.debug('detected update from webhook: %s', h)
                    changed_heads.append( (r, b) )
                    last_mtime[h] = p.stat().st_mtime

        proc_needed = []
        for c in changed_heads:
            rev = update_head(*c)
            proc_needed.append( (*c, rev) )

        # TODO: might want to sort proc_needed so that, e.g., SWMH-BETA < MiniSWMH < EMF

        for pn in proc_needed:
            process_head_change(*pn)
        

def main():
    context = daemon.DaemonContext()
    context.working_directory = str(g_base_dir)

    # we'll be running as the `daemon_username` under its default group
    pwent = pwd.getpwnam(g_daemon_user)
    context.uid = pwent[2]
    context.gid = pwent[3]

    if g_pidfile_path.exists():
        fatal('hiphub appears to already be running or to have terminated ungracefully')

    context.pidfile = lockfile.FileLock(str(g_pidfile_path))

    context.signal_map = {
        signal.SIGTERM: shutdown_daemon,
        signal.SIGHUP: shutdown_daemon,
        }

    context.umask = 0o002
    
    # for debugging purposes, we may want to NOT detach our
    # stdout/stderr, so use -F for foreground mode (even though it's
    # not really in the foreground-- still a daemon)
    if '-F' in sys.argv[1:]:
        context.stdout = sys.stdout
        context.stderr = sys.stderr
    
    with context:
        # initialize logging
        logging.basicConfig(filename=str(g_logfile_path),
                            format='%(asctime)s> %(levelname)s: %(message)s',
                            datefmt='%Y/%m/%d %H:%M:%S',
                            level=logging.DEBUG)

        # and on with the show!
        try:
            logging.info('hiphub v{} starting with pid {}...'.format(VERSION, os.getpid()))
            init_daemon()
            run_daemon()
        except Exception as e:
            # will direct a traceback to the log
            logging.exception("unhandled exception, hiphub must terminate:")
            sys.exit(255)


if __name__ == '__main__':
    main()