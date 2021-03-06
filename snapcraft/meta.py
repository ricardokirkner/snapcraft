# -*- Mode:Python; indent-tabs-mode:nil; tab-width:4 -*-
#
# Copyright (C) 2016 Canonical Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 3 as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import os
import logging
import re
import shlex
import shutil
import subprocess
import tempfile

import yaml

from snapcraft import (
    common,
)

logger = logging.getLogger(__name__)


_MANDATORY_PACKAGE_KEYS = [
    'name',
    'version',
    'description',
    'summary',
]

_OPTIONAL_PACKAGE_KEYS = [
    'architectures',
    'type',
    'license-agreement',
    'license-version',
    'plugs',
    'slots',
]

_OPTIONAL_HOOKS = [
    'config',
]

_KERNEL_KEYS = {
    'kernel': 'vmlinuz',
    'initrd': 'initrd.img',
    'modules': 'lib/modules/{}',
    'firmware': 'lib/firmware',
    'dtbs': 'dtbs',
}


class CommandError(Exception):
    pass


def create(config_data):
    """Create snap.yaml and necessary package hooks.
    Create  the meta directory and provision it with snap.yaml and hooks
    in the snap dir using information from config_data.

    :param dict config_data: project values defined in snapcraft.yaml.
    :return: meta_dir.
    """
    meta_dir = os.path.join(common.get_snapdir(), 'meta')
    os.makedirs(meta_dir, exist_ok=True)

    _write_snap_yaml(meta_dir, config_data)
    _setup_assets(meta_dir, config_data)

    return meta_dir


def _write_snap_yaml(meta_dir, config_data):
    package_snap_path = os.path.join(meta_dir, 'snap.yaml')
    snap_yaml = _compose_snap_yaml(meta_dir, config_data)

    with open(package_snap_path, 'w') as f:
        yaml.dump(snap_yaml, stream=f, default_flow_style=False)

    return snap_yaml


def _setup_assets(meta_dir, config_data):
    if any(key in config_data for key in _OPTIONAL_HOOKS):
        hooks_dir = os.path.join(meta_dir, 'hooks')
        os.makedirs(hooks_dir, exist_ok=True)

    if 'config' in config_data:
        _setup_config_hook(hooks_dir, config_data['config'])

    if 'license' in config_data:
        license_path = os.path.join(meta_dir, 'license.txt')
        shutil.copyfile(config_data['license'], license_path)

    if 'icon' in config_data:
        icon_ext = config_data['icon'].split(os.path.extsep)[1]
        icon_path = os.path.join(meta_dir, 'icon.{}'.format(icon_ext))
        shutil.copyfile(config_data['icon'], icon_path)


def _setup_config_hook(hooks_dir, config):
    config_hook_path = os.path.join(hooks_dir, 'config')

    execwrap = _wrap_exe(config)
    os.rename(os.path.join(common.get_snapdir(), execwrap), config_hook_path)


def _compose_snap_yaml(meta_dir, config_data):
    """Creates a new dictionary from config_data to obtain snap.yaml.

    Missing key exceptions will be raised if config_data does not contain all
    the _MANDATORY_PACKAGE_KEYS, config_data can be validated against the
    snapcraft schema.

    Keys that are in _OPTIONAL_PACKAGE_KEYS are ignored if not there.
    """
    snap_yaml = {}

    for key_name in _MANDATORY_PACKAGE_KEYS:
        snap_yaml[key_name] = config_data[key_name]

    for key_name in _OPTIONAL_PACKAGE_KEYS:
        if key_name in config_data:
            snap_yaml[key_name] = config_data[key_name]

    if 'apps' in config_data:
        snap_yaml['apps'] = _wrap_apps(config_data['apps'])

    if config_data.get('type', '') == 'kernel':
        snap_yaml.update(_get_kernel_keys())

    if 'uses' in config_data:
        snap_yaml['uses'] = \
            _copy_security_profiles(meta_dir, config_data['uses'])

    return snap_yaml


def _get_kernel_keys():
    kernel_keys = _KERNEL_KEYS.copy()
    version = os.listdir(os.path.join(common.get_snapdir(), 'lib', 'modules'))
    kernel_keys['modules'] = kernel_keys['modules'].format(version[0])

    return kernel_keys


def _copy(meta_dir, relpath, new_relpath=None):
    new_base = new_relpath or os.path.basename(relpath)
    target_path = os.path.join(meta_dir, new_base)

    shutil.copyfile(relpath, target_path)

    return os.path.join('meta', os.path.basename(relpath))


def _copy_security_profiles(meta_dir, uses):
    # TODO: remove once skills are implemented.
    for slot_name in uses:
        slot_type = uses[slot_name].get('type', '')
        if slot_type != 'migration-skill' and slot_name != 'migration-skill':
            continue
        if 'security-policy' in uses[slot_name]:
            uses[slot_name]['security-policy']['apparmor'] = \
                _copy(meta_dir, uses[slot_name]['security-policy']['apparmor'])
            uses[slot_name]['security-policy']['seccomp'] = \
                _copy(meta_dir, uses[slot_name]['security-policy']['seccomp'])

    return uses


def _write_wrap_exe(wrapexec, wrappath, shebang=None, args=None, cwd=None):
    args = ' '.join(args) + ' $*' if args else '$*'
    cwd = 'cd {}'.format(cwd) if cwd else ''

    snap_dir = common.get_snapdir()
    assembled_env = common.assemble_env().replace(snap_dir, '$SNAP')
    replace_path = r'{}/[a-z0-9][a-z0-9+-]*/install'.format(
        common.get_partsdir())
    assembled_env = re.sub(replace_path, '$SNAP', assembled_env)
    executable = '"{}"'.format(wrapexec)
    if shebang is not None:
        new_shebang = re.sub(replace_path, '$SNAP', shebang)
        if new_shebang != shebang:
            # If the shebang was pointing to and executable within the
            # local 'parts' dir, have the wrapper script execute it
            # directly, since we can't use $SNAP in the shebang itself.
            executable = '"{}" "{}"'.format(new_shebang, wrapexec)
    script = ('#!/bin/sh\n' +
              '{}\n'.format(assembled_env) +
              '{}\n'.format(cwd) +
              'exec {} {}\n'.format(executable, args))

    with open(wrappath, 'w+') as f:
        f.write(script)

    os.chmod(wrappath, 0o755)


def _wrap_exe(command, basename=None):
    execparts = shlex.split(command)
    snap_dir = common.get_snapdir()
    exepath = os.path.join(snap_dir, execparts[0])
    if basename:
        wrappath = os.path.join(snap_dir, basename) + '.wrapper'
    else:
        wrappath = exepath + '.wrapper'
    shebang = None

    if os.path.exists(wrappath):
        os.remove(wrappath)

    wrapexec = '$SNAP/{}'.format(execparts[0])
    if not os.path.exists(exepath) and '/' not in execparts[0]:
        _find_bin(execparts[0], snap_dir)
        wrapexec = execparts[0]
    else:
        with open(exepath, 'rb') as exefile:
            # If the file has a she-bang, the path might be pointing to
            # the local 'parts' dir. Extract it so that _write_wrap_exe
            # will have a chance to rewrite it.
            if exefile.read(2) == b'#!':
                shebang = exefile.readline().strip().decode('utf-8')

    _write_wrap_exe(wrapexec, wrappath, shebang=shebang, args=execparts[1:])

    return os.path.relpath(wrappath, snap_dir)


def _find_bin(binary, basedir):
    # If it doesn't exist it might be in the path
    logger.debug('Checking that {!r} is in the $PATH'.format(binary))
    script = ('#!/bin/sh\n' +
              '{}\n'.format(common.assemble_env()) +
              'which "{}"\n'.format(binary))
    with tempfile.NamedTemporaryFile('w+') as tempf:
        tempf.write(script)
        tempf.flush()
        try:
            common.run(['/bin/sh', tempf.name], cwd=basedir,
                       stdout=subprocess.DEVNULL)
        except subprocess.CalledProcessError:
            raise CommandError(binary)


def _wrap_apps(apps):
    for app in apps:
        for k in [k for k in ('command', 'stop-command') if k in apps[app]]:
            try:
                apps[app][k] = _wrap_exe(apps[app][k], '{}-{}'.format(k, app))
            except CommandError as e:
                raise EnvironmentError(
                    'The specified command {!r} defined in {!r} does '
                    'not exist or is not executable'.format(str(e), app))
    return apps
