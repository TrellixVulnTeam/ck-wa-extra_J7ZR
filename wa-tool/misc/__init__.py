#    Copyright 2013-2015 ARM Limited
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#


# pylint: disable=W0613,no-member,attribute-defined-outside-init
"""

Some "standard" instruments to collect additional info about workload execution.

.. note:: The run() method of a Workload may perform some "boilerplate" as well as
          the actual execution of the workload (e.g. it may contain UI automation
          needed to start the workload). This "boilerplate" execution will also
          be measured by these instruments. As such, they are not suitable for collected
          precise data about specific operations.
"""
import os
import re
import logging
import time
import tarfile
from itertools import izip, izip_longest
from subprocess import CalledProcessError

from wlauto import Instrument, Parameter
from wlauto.core import signal
from wlauto.exceptions import DeviceError, ConfigError
from wlauto.utils.misc import diff_tokens, write_table, check_output, as_relative
from wlauto.utils.misc import ensure_file_directory_exists as _f
from wlauto.utils.misc import ensure_directory_exists as _d
from wlauto.utils.android import ApkInfo
from wlauto.utils.types import list_of_strings


logger = logging.getLogger(__name__)


class SysfsExtractor(Instrument):

    name = 'sysfs_extractor'
    description = """
    Collects the contest of a set of directories, before and after workload execution
    and diffs the result.

    """

    mount_command = 'mount -t tmpfs -o size={} tmpfs {}'
    extract_timeout = 30
    tarname = 'sysfs.tar'
    DEVICE_PATH = 0
    BEFORE_PATH = 1
    AFTER_PATH = 2
    DIFF_PATH = 3

    parameters = [
        Parameter('paths', kind=list_of_strings, mandatory=True,
                  description="""A list of paths to be pulled from the device. These could be directories
                                as well as files.""",
                  global_alias='sysfs_extract_dirs'),
        Parameter('use_tmpfs', kind=bool, default=None,
                  description="""
                  Specifies whether tmpfs should be used to cache sysfile trees and then pull them down
                  as a tarball. This is significantly faster then just copying the directory trees from
                  the device directly, bur requres root and may not work on all devices. Defaults to
                  ``True`` if the device is rooted and ``False`` if it is not.
                  """),
        Parameter('tmpfs_mount_point', default=None,
                  description="""Mount point for tmpfs partition used to store snapshots of paths."""),
        Parameter('tmpfs_size', default='32m',
                  description="""Size of the tempfs partition."""),
    ]

    def initialize(self, context):
        if not self.device.is_rooted and self.use_tmpfs:  # pylint: disable=access-member-before-definition
            raise ConfigError('use_tempfs must be False for an unrooted device.')
        elif self.use_tmpfs is None:  # pylint: disable=access-member-before-definition
            self.use_tmpfs = self.device.is_rooted

        if self.use_tmpfs:
            self.on_device_before = self.device.path.join(self.tmpfs_mount_point, 'before')
            self.on_device_after = self.device.path.join(self.tmpfs_mount_point, 'after')

            if not self.device.file_exists(self.tmpfs_mount_point):
                self.device.execute('mkdir -p {}'.format(self.tmpfs_mount_point), as_root=True)
                self.device.execute(self.mount_command.format(self.tmpfs_size, self.tmpfs_mount_point),
                                    as_root=True)

    def setup(self, context):
        before_dirs = [
            _d(os.path.join(context.output_directory, 'before', self._local_dir(d)))
            for d in self.paths
        ]
        after_dirs = [
            _d(os.path.join(context.output_directory, 'after', self._local_dir(d)))
            for d in self.paths
        ]
        diff_dirs = [
            _d(os.path.join(context.output_directory, 'diff', self._local_dir(d)))
            for d in self.paths
        ]
        self.device_and_host_paths = zip(self.paths, before_dirs, after_dirs, diff_dirs)

        if self.use_tmpfs:
            for d in self.paths:
                before_dir = self.device.path.join(self.on_device_before,
                                                   self.device.path.dirname(as_relative(d)))
                after_dir = self.device.path.join(self.on_device_after,
                                                  self.device.path.dirname(as_relative(d)))
                if self.device.file_exists(before_dir):
                    self.device.execute('rm -rf  {}'.format(before_dir), as_root=True)
                self.device.execute('mkdir -p {}'.format(before_dir), as_root=True)
                if self.device.file_exists(after_dir):
                    self.device.execute('rm -rf  {}'.format(after_dir), as_root=True)
                self.device.execute('mkdir -p {}'.format(after_dir), as_root=True)

    def slow_start(self, context):
        if self.use_tmpfs:
            for d in self.paths:
                dest_dir = self.device.path.join(self.on_device_before, as_relative(d))
                if '*' in dest_dir:
                    dest_dir = self.device.path.dirname(dest_dir)
                self.device.execute('{} cp -Hr {} {}'.format(self.device.busybox, d, dest_dir),
                                    as_root=True, check_exit_code=False)
        else:  # not rooted
            for dev_dir, before_dir, _, _ in self.device_and_host_paths:
                self.device.pull_file(dev_dir, before_dir)

    def slow_stop(self, context):
        if self.use_tmpfs:
            for d in self.paths:
                dest_dir = self.device.path.join(self.on_device_after, as_relative(d))
                if '*' in dest_dir:
                    dest_dir = self.device.path.dirname(dest_dir)
                self.device.execute('{} cp -Hr {} {}'.format(self.device.busybox, d, dest_dir),
                                    as_root=True, check_exit_code=False)
        else:  # not using tmpfs
            for dev_dir, _, after_dir, _ in self.device_and_host_paths:
                self.device.pull_file(dev_dir, after_dir)

    def update_result(self, context):
        if self.use_tmpfs:
            on_device_tarball = self.device.path.join(self.device.working_directory, self.tarname)
            on_host_tarball = self.device.path.join(context.output_directory, self.tarname + ".gz")
            self.device.execute('{} tar cf {} -C {} .'.format(self.device.busybox,
                                                              on_device_tarball,
                                                              self.tmpfs_mount_point),
                                as_root=True)
            self.device.execute('chmod 0777 {}'.format(on_device_tarball), as_root=True)
            self.device.execute('{} gzip -f {}'.format(self.device.busybox,
                                                       on_device_tarball))
            self.device.pull_file(on_device_tarball + ".gz", on_host_tarball)
            with tarfile.open(on_host_tarball, 'r:gz') as tf:
                def is_within_directory(directory, target):
                    
                    abs_directory = os.path.abspath(directory)
                    abs_target = os.path.abspath(target)
                
                    prefix = os.path.commonprefix([abs_directory, abs_target])
                    
                    return prefix == abs_directory
                
                def safe_extract(tar, path=".", members=None, *, numeric_owner=False):
                
                    for member in tar.getmembers():
                        member_path = os.path.join(path, member.name)
                        if not is_within_directory(path, member_path):
                            raise Exception("Attempted Path Traversal in Tar File")
                
                    tar.extractall(path, members, numeric_owner=numeric_owner) 
                    
                
                safe_extract(tf, context.output_directory)
            self.device.delete_file(on_device_tarball + ".gz")
            os.remove(on_host_tarball)

        for paths in self.device_and_host_paths:
            after_dir = paths[self.AFTER_PATH]
            dev_dir = paths[self.DEVICE_PATH].strip('*')  # remove potential trailing '*'
            if (not os.listdir(after_dir) and
                    self.device.file_exists(dev_dir) and
                    self.device.listdir(dev_dir)):
                self.logger.error('sysfs files were not pulled from the device.')
                self.device_and_host_paths.remove(paths)  # Path is removed to skip diffing it
        for _, before_dir, after_dir, diff_dir in self.device_and_host_paths:
            _diff_sysfs_dirs(before_dir, after_dir, diff_dir)

    def teardown(self, context):
        self._one_time_setup_done = []

    def finalize(self, context):
        if self.use_tmpfs:
            try:
                self.device.execute('umount {}'.format(self.tmpfs_mount_point), as_root=True)
            except (DeviceError, CalledProcessError):
                # assume a directory but not mount point
                pass
            self.device.execute('rm -rf {}'.format(self.tmpfs_mount_point),
                                as_root=True, check_exit_code=False)

    def validate(self):
        if not self.tmpfs_mount_point:  # pylint: disable=access-member-before-definition
            self.tmpfs_mount_point = self.device.path.join(self.device.working_directory, 'temp-fs')

    def _local_dir(self, directory):
        return os.path.dirname(as_relative(directory).replace(self.device.path.sep, os.sep))


class ExecutionTimeInstrument(Instrument):

    name = 'execution_time'
    description = """
    Measure how long it took to execute the run() methods of a Workload.

    """

    priority = 15

    def __init__(self, device, **kwargs):
        super(ExecutionTimeInstrument, self).__init__(device, **kwargs)
        self.start_time = None
        self.end_time = None

    def on_run_start(self, context):
        signal.connect(self.get_start_time, signal.BEFORE_WORKLOAD_EXECUTION, priority=self.priority)
        signal.connect(self.get_stop_time, signal.AFTER_WORKLOAD_EXECUTION, priority=self.priority)

    def get_start_time(self, context):
        self.start_time = time.time()

    def get_stop_time(self, context):
        self.end_time = time.time()

    def update_result(self, context):
        execution_time = self.end_time - self.start_time
        context.result.add_metric('execution_time', execution_time, 'seconds')


class InterruptStatsInstrument(Instrument):

    name = 'interrupts'
    description = """
    Pulls the ``/proc/interrupts`` file before and after workload execution and diffs them
    to show what interrupts  occurred during that time.

    """

    def __init__(self, device, **kwargs):
        super(InterruptStatsInstrument, self).__init__(device, **kwargs)
        self.before_file = None
        self.after_file = None
        self.diff_file = None

    def setup(self, context):
        self.before_file = os.path.join(context.output_directory, 'before', 'proc', 'interrupts')
        self.after_file = os.path.join(context.output_directory, 'after', 'proc', 'interrupts')
        self.diff_file = os.path.join(context.output_directory, 'diff', 'proc', 'interrupts')

    def start(self, context):
        with open(_f(self.before_file), 'w') as wfh:
            wfh.write(self.device.execute('cat /proc/interrupts'))

    def stop(self, context):
        with open(_f(self.after_file), 'w') as wfh:
            wfh.write(self.device.execute('cat /proc/interrupts'))

    def update_result(self, context):
        # If workload execution failed, the after_file may not have been created.
        if os.path.isfile(self.after_file):
            _diff_interrupt_files(self.before_file, self.after_file, _f(self.diff_file))


class DynamicFrequencyInstrument(SysfsExtractor):

    name = 'cpufreq'
    description = """
    Collects dynamic frequency (DVFS) settings before and after workload execution.

    """

    tarname = 'cpufreq.tar'

    parameters = [
        Parameter('paths', mandatory=False, override=True),
    ]

    def setup(self, context):
        self.paths = ['/sys/devices/system/cpu']
        if self.use_tmpfs:
            self.paths.append('/sys/class/devfreq/*')  # the '*' would cause problems for adb pull.
        super(DynamicFrequencyInstrument, self).setup(context)

    def validate(self):
        # temp-fs would have been set in super's validate, if not explicitly specified.
        if not self.tmpfs_mount_point.endswith('-cpufreq'):  # pylint: disable=access-member-before-definition
            self.tmpfs_mount_point += '-cpufreq'


def _diff_interrupt_files(before, after, result):  # pylint: disable=R0914
    output_lines = []
    with open(before) as bfh:
        with open(after) as ofh:
            for bline, aline in izip(bfh, ofh):
                bchunks = bline.strip().split()
                while True:
                    achunks = aline.strip().split()
                    if achunks[0] == bchunks[0]:
                        diffchunks = ['']
                        diffchunks.append(achunks[0])
                        diffchunks.extend([diff_tokens(b, a) for b, a
                                           in zip(bchunks[1:], achunks[1:])])
                        output_lines.append(diffchunks)
                        break
                    else:  # new category appeared in the after file
                        diffchunks = ['>'] + achunks
                        output_lines.append(diffchunks)
                        try:
                            aline = ofh.next()
                        except StopIteration:
                            break

    # Offset heading columns by one to allow for row labels on subsequent
    # lines.
    output_lines[0].insert(0, '')

    # Any "columns" that do not have headings in the first row are not actually
    # columns -- they are a single column where space-spearated words got
    # split. Merge them back together to prevent them from being
    # column-aligned by write_table.
    table_rows = [output_lines[0]]
    num_cols = len(output_lines[0])
    for row in output_lines[1:]:
        table_row = row[:num_cols]
        table_row.append(' '.join(row[num_cols:]))
        table_rows.append(table_row)

    with open(result, 'w') as wfh:
        write_table(table_rows, wfh)


def _diff_sysfs_dirs(before, after, result):  # pylint: disable=R0914
    before_files = []
    os.path.walk(before,
                 lambda arg, dirname, names: arg.extend([os.path.join(dirname, f) for f in names]),
                 before_files
                 )
    before_files = filter(os.path.isfile, before_files)
    files = [os.path.relpath(f, before) for f in before_files]
    after_files = [os.path.join(after, f) for f in files]
    diff_files = [os.path.join(result, f) for f in files]

    for bfile, afile, dfile in zip(before_files, after_files, diff_files):
        if not os.path.isfile(afile):
            logger.debug('sysfs_diff: {} does not exist or is not a file'.format(afile))
            continue

        with open(bfile) as bfh, open(afile) as afh:  # pylint: disable=C0321
            with open(_f(dfile), 'w') as dfh:
                for i, (bline, aline) in enumerate(izip_longest(bfh, afh), 1):
                    if aline is None:
                        logger.debug('Lines missing from {}'.format(afile))
                        break
                    bchunks = re.split(r'(\W+)', bline)
                    achunks = re.split(r'(\W+)', aline)
                    if len(bchunks) != len(achunks):
                        logger.debug('Token length mismatch in {} on line {}'.format(bfile, i))
                        dfh.write('xxx ' + bline)
                        continue
                    if ((len([c for c in bchunks if c.strip()]) == len([c for c in achunks if c.strip()]) == 2) and
                            (bchunks[0] == achunks[0])):
                        # if there are only two columns and the first column is the
                        # same, assume it's a "header" column and do not diff it.
                        dchunks = [bchunks[0]] + [diff_tokens(b, a) for b, a in zip(bchunks[1:], achunks[1:])]
                    else:
                        dchunks = [diff_tokens(b, a) for b, a in zip(bchunks, achunks)]
                    dfh.write(''.join(dchunks))
