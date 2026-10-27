"""Windows micromamba launcher: activate without CMD, then preserve native argv/stdio.

The environment snapshot exists only in memory. Backend activation assignments
are decoded as literal data; only installed package hook files are executed.
"""
import base64
import json
import os
import re
import shutil
import subprocess
import sys


_ACTIVATION_CAPTURE = r"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::InputEncoding = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$actions = ConvertFrom-Json -InputObject ([Console]::In.ReadToEnd())
foreach ($action in $actions) {
    switch ($action.kind) {
        'set' {
            [Environment]::SetEnvironmentVariable([string]$action.name, [string]$action.value, 'Process')
        }
        'hook' {
            $null = . ([string]$action.path)
        }
        default { throw 'Unsupported activation action.' }
    }
}
$environment = @{}
foreach ($item in [Environment]::GetEnvironmentVariables('Process').GetEnumerator()) {
    $environment[$item.Key] = $item.Value
}
[Console]::Out.Write((ConvertTo-Json -InputObject $environment -Compress))
"""


def native_micromamba(executable):
    """Resolve standard condabin wrappers without evaluating batch files."""
    if os.path.splitext(executable)[1].lower() == '.exe' and os.path.isfile(executable):
        return executable
    if os.path.splitext(executable)[1].lower() not in ('.bat', '.cmd'):
        found = shutil.which(executable)
        if found and found.lower().endswith('.exe'):
            return found
        if found:
            executable = found
    directory = os.path.dirname(os.path.abspath(executable))
    name = os.path.splitext(os.path.basename(executable))[0] + '.exe'
    root = os.path.dirname(directory) if os.path.basename(directory).lower() in ('condabin', 'scripts', 'bin') else directory
    candidates = [os.path.join(directory, name), os.path.join(root, name),
                  os.path.join(root, 'Library', 'bin', name), os.path.join(root, 'Scripts', name)]
    for path in candidates:
        if os.path.isfile(path):
            return path
    raise RuntimeError('Cannot find native micromamba.exe beside the configured backend. '
                       'Set backend_executables.micromamba to the actual micromamba.exe.')


def powershell_executable():
    system_root = os.environ.get('SystemRoot', r'C:\Windows')
    path = os.path.join(system_root, 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe')
    if not os.path.isfile(path):
        raise RuntimeError('Windows PowerShell is required to activate micromamba without CMD.')
    return path


def activation_actions(source, prefixes):
    """Read micromamba's fixed activation format as data, never PowerShell code.

    Micromamba 2.4 emits unescaped double-quoted values. Evaluating them would
    expand dollars/backticks or execute $(). Unknown formats fail closed instead
    of falling back to eval. Hook paths must be installed activation scripts.
    """
    hook_paths = {}
    for prefix in prefixes:
        if not prefix:
            continue
        for kind in ('activate.d', 'deactivate.d'):
            directory = os.path.join(prefix, 'etc', 'conda', kind)
            if os.path.isdir(directory):
                for name in os.listdir(directory):
                    path = os.path.join(directory, name)
                    if name.lower().endswith('.ps1') and os.path.isfile(path):
                        hook_paths[os.path.normcase(os.path.realpath(path))] = path
    try:
        text = source.decode('utf-8-sig')
    except UnicodeError:
        raise RuntimeError('micromamba returned invalid UTF-8 activation data.') from None
    actions = []
    for line in text.splitlines():
        if not line.strip():
            continue
        assignment = re.fullmatch(r'\$Env:([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"(.*)"', line)
        if assignment:
            actions.append({'kind': 'set', 'name': assignment.group(1), 'value': assignment.group(2)})
            continue
        if line.startswith('. '):
            path = line[2:].strip()
            if len(path) >= 2 and path[0] == path[-1] and path[0] in ('"', "'"):
                path = path[1:-1]
            canonical = os.path.normcase(os.path.realpath(path))
            if canonical in hook_paths:
                actions.append({'kind': 'hook', 'path': hook_paths[canonical]})
                continue
        # Do not quote rejected source: environment values may contain secrets.
        raise RuntimeError('Unsupported micromamba activation format. No generated code was executed.')
    return actions


def activated_environment(backend, prefix):
    """Apply literal activation values and run the environment's .ps1 hooks."""
    native = native_micromamba(backend)
    parent = os.environ.copy()
    # Standard condabin wrappers set these before calling the native executable.
    parent['MAMBA_EXE'] = native
    parent.setdefault('MAMBA_ROOT_PREFIX', os.path.dirname(native))
    generated = subprocess.run(
        [native, 'shell', 'activate', '--shell', 'powershell', '--prefix', prefix],
        env=parent, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20,
    )
    if generated.returncode:
        raise RuntimeError('micromamba PowerShell activation generation failed (exit {}). '
                           'Check the backend and selected environment.'.format(generated.returncode))
    prefixes = [prefix, parent.get('CONDA_PREFIX', '')]
    for key, value in parent.items():
        if re.fullmatch(r'CONDA_PREFIX_\d+', key.upper()):
            prefixes.append(value)
    actions = activation_actions(generated.stdout, prefixes)
    encoded = base64.b64encode(_ACTIVATION_CAPTURE.encode('utf-16-le')).decode('ascii')
    captured = subprocess.run(
        [powershell_executable(), '-NoLogo', '-NoProfile', '-NonInteractive',
         '-ExecutionPolicy', 'Bypass', '-EncodedCommand', encoded],
        input=json.dumps(actions, ensure_ascii=True).encode('utf-8'),
        env=parent, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20,
    )
    if captured.returncode:
        # Do not include captured stdout: it may contain environment secrets.
        # PowerShell error records can quote activation source/secret values too.
        raise RuntimeError('PowerShell environment activation failed (exit {}). '
                           'Check the environment activation scripts.'.format(captured.returncode))
    try:
        values = json.loads(captured.stdout.decode('utf-8-sig'))
        if not isinstance(values, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in values.items()):
            raise ValueError('Invalid environment object')
    except (UnicodeError, ValueError):
        raise RuntimeError('PowerShell activation did not return a valid environment object.') from None
    environment = {key.upper(): value for key, value in values.items() if not key.startswith('=')}
    # micromamba may canonicalize junction/symlink prefixes during activation.
    if os.path.normcase(os.path.realpath(environment.get('CONDA_PREFIX', ''))) != os.path.normcase(os.path.realpath(prefix)):
        raise RuntimeError('Activation did not select the requested environment prefix.')
    # Keep Python worker protocols and Sublime's UTF-8 output decoder consistent.
    environment['PYTHONIOENCODING'] = 'utf-8'
    return environment


def resolved_command(command, environment):
    if not command:
        raise RuntimeError('No environment command was provided.')
    executable = None
    if os.path.dirname(command[0]):
        executable = shutil.which(command[0], path=environment.get('PATH', ''))
    else:
        # Search the activated PATH explicitly; Windows' implicit current-directory
        # search must not let a project-local python.exe shadow the selected env.
        for directory in environment.get('PATH', '').split(os.pathsep):
            if directory:
                executable = shutil.which(os.path.join(directory, command[0]))
                if executable:
                    break
    if not executable:
        raise RuntimeError('Command not found in the activated environment: {}'.format(command[0]))
    if executable.lower().endswith(('.bat', '.cmd')):
        raise RuntimeError('Batch target commands require CMD and cannot safely preserve arbitrary arguments. '
                           'Configure the native .exe or run the script through its interpreter: {}'.format(executable))
    return [executable] + list(command[1:])


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if len(args) < 4 or args[2] != '--':
        raise RuntimeError('Expected backend, environment prefix, --, and target command.')
    environment = activated_environment(args[0], args[1])
    command = resolved_command(args[3:], environment)
    # Inherit handles directly: no PowerShell text conversion, buffering or JSON-RPC
    # corruption. Keep this parent alive so Sublime cancellation can kill the tree.
    return subprocess.call(command, env=environment)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as error:
        sys.stderr.write('LiuConda launcher: {}\n'.format(error))
        sys.exit(1)
