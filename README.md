# LiuConda

**Lightweight IDE Utility for Conda**  
A Sublime Text 4 plugin that connects the editor to `conda` and `micromamba`
Python environments.

LiuConda turns Sublime Text into a small, environment-aware Python workspace:
pick an environment, run the current file, jump to definitions, read
documentation, and ask Zuban for type diagnostics without leaving the editor.

I mainly use Windows 11, and although I have tried to keep LiuConda as
cross-platform as possible, Linux and macOS support is not guaranteed.
Pull requests that improve cross-platform compatibility are very welcome.

## Highlights

- Switch between `conda` and `micromamba`.
- Discover environments from the selected backend.
- Run the current Python file inside the selected environment.
- Keep duplicate environment names distinct by storing full environment paths.
- Use Jedi for completion, signature help, documentation, and definition lookup.
- Use Zuban for on-demand type diagnostics.
- Work around editors that do not inherit the same `PATH` as your terminal.
- Hide conda-build internal environments such as `conda-bld/.../_h_env`.

## Requirements

- Sublime Text 4.
- A working `conda` or `micromamba` installation.
- Optional: install editor tools only in the environments where you want them:
  - `jedi` for editor intelligence.
  - `zuban` for type diagnostics.

## Installation

Clone or copy this package into your Sublime Text `Packages` directory.

On Windows, the package path commonly looks like:

```text
%APPDATA%\Sublime Text\Packages\LiuConda
```

Then restart Sublime Text, or run:

```text
Tools > Developer > Reload Plugins
```

## Quick Start

1. Open `LiuPlugins > LiuConda > Conda Version`.
2. Choose `conda` or `micromamba`.
3. If Sublime Text cannot find the backend, use `Set Backend Path...`.
4. Run `LiuPlugins > LiuConda > Refresh Env`.
5. Pick an environment from `LiuPlugins > LiuConda > Environments`.
6. Open a Python file and run `LiuPlugins > LiuConda > Run`.

The command palette also exposes common commands such as:

```text
LiuConda: Refresh Env
LiuConda: Go to Definition
LiuConda: Show Documentation
LiuConda: Type Diagnostics
LiuConda: Zuban Status
```

## Menu

| Menu item | What it does |
| --- | --- |
| `Conda Version` | Select `conda` or `micromamba` as the active backend. |
| `Refresh Env` | Reload environments from the selected backend. |
| `Environments` | Select the Python environment used by LiuConda. |
| `Run` | Run the current Python file in the selected environment. |
| `Go to Definition` | Jump to the symbol definition using Jedi. |
| `Show Documentation` | Show Jedi documentation for the symbol under the cursor. |
| `Hover Documentation` | Toggle mouse-hover documentation. Disabled by default. |
| `Type Diagnostics` | Run Zuban diagnostics for the current Python buffer. |
| `Jedi On/Off` | Show whether Jedi is available in the selected environment. |
| `Zuban On/Off` | Show whether Zuban is available in the selected environment. |
| `Set Backend Path...` | Manually set the executable path for the active backend. |
| `Global Path Search` | Search extra backend install locations. Disabled by default. |

## Settings

Edit `LiuConda.sublime-settings` if you need to customize behavior. Sublime
settings files may contain comments, but LiuConda keeps the default file as
plain JSON so external tools can parse it.

```json
{
    "backend": "conda",
    "backend_executables": {
        "conda": "",
        "micromamba": ""
    },
    "selected_env": "",
    "envs": [],
    "python": "python",
    "zuban": "zuban",
    "jedi_timeout": 5,
    "jedi_command_timeout": 20,
    "zuban_timeout": 10,
    "runtime_documentation_import_whitelist": [
        "numpy"
    ],
    "hover_documentation": false,
    "global_path_search": false
}
```

| Option | Description |
| --- | --- |
| `backend` | Active environment manager. Use `"conda"` or `"micromamba"`. |
| `backend_executables` | Optional executable paths for each backend. Useful when Sublime Text has a different `PATH` from your terminal. |
| `selected_env` | Selected environment identifier. LiuConda stores full paths when possible so duplicate names remain distinct. |
| `envs` | Cached environment records discovered by `Refresh Env`. Normally managed by the plugin. |
| `python` | Python command used by `Run`, usually `"python"`. |
| `zuban` | Zuban command used for diagnostics, usually `"zuban"`. |
| `jedi_timeout` | Timeout in seconds for interactive Jedi requests such as completion, signatures, and hover documentation. |
| `jedi_command_timeout` | Timeout in seconds for explicit Jedi commands such as `Go to Definition` and `Show Documentation`. |
| `zuban_timeout` | Timeout in seconds for Zuban startup and diagnostic responses. |
| `runtime_documentation_import_whitelist` | Modules LiuConda may import for runtime documentation resolution. Keep this list small and trusted. |
| `hover_documentation` | Whether mouse-hover documentation is enabled. |
| `global_path_search` | Whether LiuConda searches extra backend locations beyond `PATH` and common install paths. |

## Jedi

Jedi powers completion, signature help, documentation, and definition lookup.
LiuConda starts its Jedi worker through the selected backend, so the worker sees
the same packages as the environment you selected in Sublime Text.

Install Jedi in every environment where you want these features:

```bash
python -m pip install jedi
```

or:

```bash
conda install jedi
```

## Zuban

`Type Diagnostics` starts `zuban server` inside the selected environment, sends
the current buffer through a small built-in LSP adapter, and displays diagnostics
in a Sublime output panel.

Install Zuban in every environment where you want diagnostics:

```bash
python -m pip install zuban
```

## Troubleshooting

### Sublime Text cannot find conda or micromamba

Use:

```text
LiuPlugins > LiuConda > Set Backend Path...
```

Then point LiuConda to the backend executable, for example:

```text
C:\path\to\micromamba\condabin\micromamba.bat
```

This is common on Windows because GUI applications often start with a different
environment from your terminal.

### An unexpected environment appears

Some backends may report internal build environments created under `conda-bld`.
LiuConda filters those out during refresh so temporary paths such as
`conda-bld/.../_h_env` do not clutter the environment menu.

### Jedi or Zuban is marked unavailable

Select the environment you want to use, then install the missing package in that
same environment. For example:

```bash
micromamba run -n my-env python -m pip install jedi zuban
```

Then run `Refresh Env` or select the environment again.

## Development

Run the unit tests from a Python environment that can import the standard test
dependencies:

```bash
python -m unittest discover -s unit_test -v
```

With micromamba:

```bash
micromamba run -n mlearn python -m unittest discover -s unit_test -v
```

## Project Name

**LiuConda** stands for **Lightweight IDE Utility for Conda**.

It is designed for people who like Sublime Text's speed, but still want a few
focused IDE comforts when working across Python environments.
