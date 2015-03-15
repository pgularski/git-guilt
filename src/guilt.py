# -*- coding: UTF-8 -*-
# Copyright (c) 2015, Matt Boyer
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
#     1. Redistributions of source code must retain the above copyright notice,
#     this list of conditions and the following disclaimer.
#
#     2. Redistributions in binary form must reproduce the above copyright
#     notice, this list of conditions and the following disclaimer in the
#     documentation and/or other materials provided with the distribution.
#
#     3. Neither the name of the copyright holder nor the names of its
#     contributors may be used to endorse or promote products derived from this
#     software without specific prior written permission.
#
#     THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS
#     IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO,
#     THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR
#     PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR
#     CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL,
#     EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO,
#     PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR
#     PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF
#     LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING
#     NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
#     SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
#
"""
Port of git-guilt to Python
"""
from __future__ import print_function

import re
import os
import argparse
import subprocess
import tempfile
import collections
import functools
import sys
# Terminal stuff
import fcntl
import termios
import struct
import unicodedata


class GitError(Exception):
    pass


class GitRunner(object):
    _toplevel_args = ['rev-parse', '--show-toplevel']
    _git_executable = 'git'

    def __init__(self):
        self._git_toplevel = None
        try:
            self._get_git_root()
        except GitError as ex:
            # Do something appropriate
            Formatter.terminal_output(str(ex), sys.stderr)
            raise SystemExit(4)

    def _get_git_root(self):
        # We should probably go beyond just finding the root dir for the Git
        # repo and do some sanity-checking on git itself
        top_level_dir = self.run_git(GitRunner._toplevel_args)
        self._git_toplevel = top_level_dir[0]

    def run_git(self, args, git_env=None):
        '''
        Runs the git executable with the arguments given and returns a list of
        lines produced on its standard output.
        '''

        popen_kwargs = {
            'stdout': subprocess.PIPE,
            'stderr': subprocess.PIPE,
        }

        if git_env:
            popen_kwargs['env'] = git_env

        if self._git_toplevel:
            popen_kwargs['cwd'] = self._git_toplevel

        git_process = subprocess.Popen(
            [GitRunner._git_executable] + args,
            **popen_kwargs
        )

        try:
            out, err = git_process.communicate()
            git_process.wait()
        except Exception as e:
            raise GitError("Couldn't run 'git {args}':{newline}{ex}".format(
                args=' '.join(args),
                newline=os.linesep,
                ex=str(e)
            ))

        if (0 != git_process.returncode) or err:
            if err:
                err = err.decode('utf_8')
            raise GitError("'git {args}' failed with:{newline}{err}".format(
                args=' '.join(args),
                newline=os.linesep,
                err=err
            ))

        if not out:
            raise ValueError("No output")

        return out.decode('utf_8').splitlines()

    def get_delta_files(self, since_rev, until_rev):
        '''
        Returns a list of files which have been modified between since_rev and
        until_rev.

        :param since_rev: the old Git revision
        :type since_rev: str
        :param until_rev: the new Git revision
        :type until_rev: str
        :return: A `(text_files, binary_files)` tuple
        :rtype: tuple
        '''

        # We wanna take note of binary files and process them differently
        text_files = set()
        binary_files = set()

        diff_args = ['diff', '-z', '--numstat', since_rev]
        if until_rev:
            diff_args.append(until_rev)

        num_stat_lines = self.run_git(diff_args)
        num_stat_lines = num_stat_lines[0].split(chr(0))
        if 0 == len(num_stat_lines):
            raise ValueError()
        if not num_stat_lines[-1]:
            num_stat_lines = num_stat_lines[:-1]

        for num_stat_line in num_stat_lines:
            (additions, deletions, file_name) = num_stat_line.split('\t')
            if ('-', '-') == (additions, deletions):
                binary_files.add(file_name)
            else:
                text_files.add(file_name)

        return (text_files, binary_files)

    def populate_tree(self, rev):
        # We need to detect submodules/non-blobs
        ls_tree_args = ['ls-tree', '-r', '--', rev]
        paths = set()

        lines = self.run_git(ls_tree_args)
        for line in lines:
            _, object_type, _ = line.split('\t')[0].split()
            path = line.split('\t')[1]
            if 'blob' != object_type:
                continue
            paths.add(path)

        return paths


class BlameTicket(object):
    '''A queued blame. This is a TODO item, really'''
    _author_regex = r'^[^(]*\((.*?) \d{4}-\d{2}-\d{2}'

    def __init__(self, runner, bucket, path, rev):
        self.name_regex = re.compile(self._author_regex)

        self.runner = runner
        self.bucket = bucket
        self.repo_path = path
        self.rev = rev

        self.config_pairs = dict()
        self.config_pairs['user.name'] = 'foo'
        self.config_pairs['user.email'] = 'bar@example.com'

    def __eq__(self, blame):
        return (self.bucket == blame.bucket) \
            and (self.repo_path == blame.repo_path) \
            and (self.bucket == blame.bucket)

    def _format_config(self):
        git_config_params = list()
        for key, value in sorted(self.config_pairs.items()):
            git_config_params.append("'{config_key}={config_value}'".format(
                config_key=key,
                config_value=value
            ))
        return ' '.join(git_config_params)

    def blame_args(self):
        blame_args = ['blame', '--encoding=utf-8', '--', self.repo_path]
        if self.rev:
            blame_args.append(self.rev)
        return blame_args

    def blame_env(self):
        environment = dict()
        if self.config_pairs:
            environment['GIT_CONFIG_PARAMETERS'] = self._format_config()
        environment['GIT_CONFIG_NOSYSTEM'] = 'true'
        return environment


class TextBlameTicket(BlameTicket):
    def __init__(self, runner, bucket, path, rev):
        super(TextBlameTicket, self).__init__(runner, bucket, path, rev)

    def __repr__(self):
        return "<TextBlame {rev}:\"{path}\">".format(
            rev=self.rev,
            path=self.repo_path,
        )

    def process(self):
        '''
        Updates the bucket with a tally of the ownership of LOCs in this file
        '''

        try:
            lines = self.runner.run_git(
                self.blame_args(),
                git_env=self.blame_env(),
            )
        except GitError as ge:
            if 'no such path ' in str(ge):
                return None
            else:
                raise ge
        except ValueError as ve:
            # Not having any output is actually OK if we have an empty file
            if 'no output' in str(ve).lower():
                return

        # TODO For now default to extracting names
        for line in lines:
            matches = self.name_regex.match(line)
            if matches:
                line_author = matches.group(1).strip()
                self.bucket[line_author] += 1


class BinaryBlameTicket(BlameTicket):
    def __init__(self, runner, bucket, path, rev):
        super(BinaryBlameTicket, self).__init__(runner, bucket, path, rev)

        binary_git_config_dict = dict()
        binary_git_config_dict['diff.binary_blame.textconv'] = 'xxd -p -c1'
        binary_git_config_dict['diff.binary_blame.cachetextconv'] = 'true'

        self.config_pairs.update(binary_git_config_dict)
        self.temp_gitattributes_file = None

    def __repr__(self):
        return "<BinaryBlame {rev}:\"{path}\">".format(
            rev=self.rev,
            path=self.repo_path,
        )

    def process(self):
        '''
        Updates the bucket with a tally of the ownership of bytes in this
        binary file
        '''

        with tempfile.NamedTemporaryFile(delete=True) as temp_file:
            try:
                # We need to prepare the gitattributes file
                temp_file.write(
                    ("{binary_path} diff=binary_blame".format(
                        binary_path=self.repo_path
                    ) + os.linesep).encode('utf_8')
                )
                temp_file.flush()
                self.config_pairs.update({
                    'core.attributesfile': temp_file.name
                })
                lines = self.runner.run_git(
                    self.blame_args(),
                    git_env=self.blame_env()
                )
            except GitError as ge:
                if 'no such path ' in str(ge):
                    return None
                else:
                    raise ge

        # TODO For now default to extracting names
        for line in lines:
            matches = self.name_regex.match(line)
            if matches:
                line_author = matches.group(1).strip()
                self.bucket[line_author] += 1


class Formatter(object):
    _CSI = r'['
    _green = _CSI + '32m'
    _red = _CSI + '31m'
    _normal = _CSI + '0m'
    _default_width = 80

    def __init__(self, deltas):
        self.deltas = deltas
        self._is_tty = os.isatty(sys.stdout.fileno())
        self._tty_width = self._get_tty_width()

    @staticmethod
    def term_width(unicode_string):
        wide = 'WFA'
        return sum([2 if unicodedata.east_asian_width(c) in wide else 1
                    for c in unicode_string])

    @property
    def longest_name(self):
        return len(max(
            self.deltas, key=lambda d: Formatter.term_width(d.author)
        ).author)

    @property
    def longest_count(self):
        return len(str(max(
            self.deltas, key=lambda d: len(str(d.count))
        ).count))

    @property
    def longest_bargraph(self):
        return abs(max(
            self.deltas, key=lambda d: abs(d.count)
        ).count)

    @property
    def bargraph_max_width(self):
        return self._tty_width - (5 + self.longest_name + self.longest_count)

    @staticmethod
    def terminal_output(content, stream):
        if 2 == sys.version_info[0]:
            # TODO We need to do The Right Thing wrt. terminal encoding. It's
            # not necessarily going to be UTF-8!
            print(content.encode('utf_8'), file=stream)
        elif 3 == sys.version_info[0]:
            assert isinstance(content, str)
            # Python3 stdout/stderr file-like objects implement TextIOWrapper,
            # meaning that they expect to output native unicode strings - the
            # sys.stdout/sys.stderr objects carry their own encoding
            # information
            print(content, file=stream)

    def _get_tty_width(self):
        if not self._is_tty:
            return Formatter._default_width

        try:
            (_, w, _, _) = struct.unpack(
                'HHHH',
                fcntl.ioctl(
                    sys.stdout.fileno(),
                    termios.TIOCGWINSZ,
                    struct.pack('HHHH', 0, 0, 0, 0)
                )
            )
        except IOError:
            return Formatter._default_width

        if 0 < w:
            return w
        else:
            return Formatter._default_width

    def show_guilt_stats(self):
        # TODO Do something like diffstat's number of files changed, number or
        # insertions and number of deletions
        for delta in self.deltas:
            Formatter.terminal_output(self.format(delta), sys.stdout)

    def _scale_bargraph(self, graph_width):
        if 0 == graph_width:
            return 0

        if self.longest_bargraph <= self.bargraph_max_width:
            return graph_width

        scaled_width = 1 + \
            int(graph_width * (self.bargraph_max_width - 1) /
                self.longest_bargraph)

        return scaled_width

    def format(self, delta):
        if isinstance(delta, BinaryDelta):
            return self._format_byte_delta(delta)
        elif isinstance(delta, Delta):
            return self._format_loc_delta(delta)

    def _format_loc_delta(self, delta):
        bargraph = str()

        graph_width = self._scale_bargraph(abs(delta.count))

        if delta.count > 0:
            bargraph = '+' * graph_width
            if self._is_tty:
                bargraph = Formatter._green + bargraph + Formatter._normal
        elif delta.count < 0:
            bargraph = '-' * graph_width
            if self._is_tty:
                bargraph = Formatter._red + bargraph + Formatter._normal

        return u" {author} | {count} {bargraph}".format(
            author=delta.author.ljust(
                self.longest_name - Formatter.term_width(delta.author) +
                len(delta.author)
            ),
            count=str(delta.count).rjust(self.longest_count),
            bargraph=bargraph,
        )

    def _format_byte_delta(self, delta):
        return u" {author} | {count} ({since}->{until}) bytes".format(
            author=delta.author.ljust(
                self.longest_name - Formatter.term_width(delta.author) +
                len(delta.author)
            ),
            count=str(delta.count).rjust(self.longest_count),
            since=delta.since_locs,
            until=delta.until_locs,
        )


class Delta(object):
    '''
    Keeps track of an author's share in the ownership of text file LOCs across
    all files in the repository.
    '''

    def __init__(self, author, since, until):
        self.author = author
        self.since_locs = since
        self.until_locs = until

    def __repr__(self):
        return "<Delta \"{author}\": {count} ({since}->{until})>".format(
            author=self.author,
            count=self.count,
            since=self.since_locs,
            until=self.until_locs,
        )

    @property
    def count(self):
        return self.until_locs - self.since_locs

    def __eq__(self, rhs):
        return (self.author == rhs.author) \
            and (self.count == rhs.count)

    def __ne__(self, rhs):
        return not (self == rhs)

    def __lt__(self, rhs):
        if self.count > rhs.count:
            return True
        elif self.count == rhs.count:
            # Compare the authors' names
            return self.author < rhs.author
        else:
            return False

    def __le__(self, rhs):
        return (self < rhs) or (self == rhs)

    def __gt__(self, delta):
        if self.count < delta.count:
            return True
        elif self.count == delta.count:
            # Compare the authors' names
            return self.author > delta.author
        else:
            return False

    def __ge__(self, rhs):
        return (self > rhs) or (self == rhs)


class BinaryDelta(Delta):
    '''
    Keeps track of an author's share in the ownership of binary file bytes
    across all files in the repository.
    '''

    def __init__(self, author, since, until):
        super(BinaryDelta, self).__init__(author, since, until)

    def __repr__(self):
        return "<BinaryDelta \"{author}\": {count} ({since}->{until})>".format(
            author=self.author,
            count=self.count,
            since=self.since_locs,
            until=self.until_locs,
        )


class PyGuilt(object):
    '''
    Implements the ownership tracking logic
    '''

    def __init__(self):
        self.parser = setup_argparser()
        self.args = None

        # Job queue for the 'git blame' executions
        self.blame_jobs = list()

        # Set up ownership buckets for the "since" and "until" revisions
        # Note: binary and text ownership are fundamentally different (you
        # can't compare LOCs and individual bytes) and so should be accounted
        # for separately
        self.loc_ownership_since = collections.defaultdict(int)
        self.loc_ownership_until = collections.defaultdict(int)

        self.byte_ownership_since = collections.defaultdict(int)
        self.byte_ownership_until = collections.defaultdict(int)

        # The relative change in ownership of text file LOCs/binary file byte
        # for every author. The objects in these lists can be sorted sensibly
        self.loc_deltas = list()
        self.byte_deltas = list()

        # Dictionary
        # - keys are the "since" and "until" Git revision pointers
        # given on the CLI
        # - values are sets of relative paths (as unicode strings) for all
        # regular files present in the repo for that revision
        self.trees = dict()

        # Helper objects
        self.runner = GitRunner()
        self.loc_formatter = Formatter(self.loc_deltas)
        self.byte_formatter = Formatter(self.byte_deltas)

    def process_args(self):
        self.args = self.parser.parse_args()
        if not (self.args.since and self.args.until):
            raise GitError('Invalid arguments')

    def populate_trees(self):
        '''
        Populates self.tree with the set of regular files present in the
        version of the repo described by self.args.since and self.args.until
        '''

        self.trees[self.args.since] = self.runner.populate_tree(
            self.args.since
        )
        self.trees[self.args.until] = self.runner.populate_tree(
            self.args.until
        )

    def map_blames(self):
        '''
        Discovers the set of files that have changed between the Git revision
        pointed to by the `since` CLI arg and the `until` Git revision

        For each file, adds a blame ticket to self.blame_jobs of the
        appropriate type (text or binary) for the since and until revision.
        '''

        text_files, binary_files = self.runner.get_delta_files(
            self.args.since, self.args.until
        )

        for repo_path in sorted(text_files):
            self.blame_jobs.append(
                TextBlameTicket(
                    self.runner,
                    self.loc_ownership_since,
                    repo_path,
                    self.args.since
                )
            )

            self.blame_jobs.append(
                TextBlameTicket(
                    self.runner,
                    self.loc_ownership_until,
                    repo_path,
                    self.args.until
                )
            )

        for repo_path in sorted(binary_files):
            self.blame_jobs.append(
                BinaryBlameTicket(
                    self.runner,
                    self.byte_ownership_since,
                    repo_path,
                    self.args.since
                )
            )

            self.blame_jobs.append(
                BinaryBlameTicket(
                    self.runner,
                    self.byte_ownership_until,
                    repo_path,
                    self.args.until
                )
            )

        # Process all blame tickets in the self.blame_jobs queue
        # TODO This should be made parallel
        for blame in self.blame_jobs:
            # FIXME This should be moved to the job enqueueing routine -
            # there's no point having jobs we're not gonna process
            if blame.repo_path in self.trees[blame.rev]:
                blame.process()

    def _reduce_since_text_blame(self, deltas, since_blame):
        author, loc_count = since_blame
        until_loc_count = self.loc_ownership_until[author] or 0
        deltas.append(Delta(author, loc_count, until_loc_count))
        return deltas

    def _reduce_since_byte_blame(self, deltas, since_blame):
        author, byte_count = since_blame
        until_byte_count = self.byte_ownership_until[author] or 0
        deltas.append(BinaryDelta(author, byte_count, until_byte_count))
        return deltas

    def _reduce_until_text_blame(self, deltas, until_blame):
        author, loc_count = until_blame
        if author not in self.loc_ownership_since:
            # We have a new author
            deltas.append(Delta(author, 0, loc_count))
        return deltas

    def _reduce_until_byte_blame(self, deltas, until_blame):
        author, byte_count = until_blame
        if author not in self.byte_ownership_since:
            # We have a new author
            deltas.append(BinaryDelta(author, 0, byte_count))
        return deltas

    def reduce_blames(self):
        self._reduce_text_blames()
        self._reduce_byte_blames()

    def _reduce_text_blames(self):
        self.loc_deltas = functools.reduce(
            self._reduce_since_text_blame,
            self.loc_ownership_since.items(),
            self.loc_deltas
        )

        self.loc_deltas = functools.reduce(
            self._reduce_until_text_blame,
            self.loc_ownership_until.items(),
            self.loc_deltas
        )
        self.loc_deltas.sort()

    def _reduce_byte_blames(self):
        self.byte_deltas = functools.reduce(
            self._reduce_since_byte_blame,
            self.byte_ownership_since.items(),
            self.byte_deltas
        )

        self.byte_deltas = functools.reduce(
            self._reduce_until_byte_blame,
            self.byte_ownership_until.items(),
            self.byte_deltas
        )

        self.byte_deltas.sort()

    def run(self):
        try:
            self.process_args()
        except GitError as ex:
            Formatter.terminal_output(str(ex), sys.stderr)
            return 1
        else:
            self.populate_trees()
            self.map_blames()
            self.reduce_blames()
            self.loc_formatter.show_guilt_stats()
            if self.byte_deltas:
                # Insert an empty line before binary file guilt stats are shown
                Formatter.terminal_output('', sys.stdout)
                self.byte_formatter.show_guilt_stats()
            return 0


def main():
    sys.exit(PyGuilt().run())


def setup_argparser():
    '''
    Returns an instance of argparse.ArgumentParser for git-guilt
    '''

    parser = argparse.ArgumentParser(prog='git guilt')
    parser.add_argument(
        '-w',
        '--whitespace',
        action='store_true',
    )
    parser.add_argument('-e', '--email', action='store_true')
    parser.add_argument('since', nargs='?')
    # Surely until should default to something sensible
    parser.add_argument('until', nargs='?')
    return parser

if '__main__' == __name__:
    main()
