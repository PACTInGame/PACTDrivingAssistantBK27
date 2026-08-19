"""Every module of the add-on must import on a machine without LFS.

This is the guard that keeps the rest of the suite possible: the moment a
module-level ``import pyautogui`` (or winsound, pynput, pygame, tkinter) comes
back, nothing below it can be tested any more.
"""

import ast
import importlib
import os
import pkgutil

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The packages that make up the running application.
PACKAGES = ('assistance', 'core', 'lfs', 'misc', 'ui', 'vehicles')

# Root-level modules that are part of the app (MapBuilder.py, test.py and
# AI_Cheatsheet.py are offline tooling / dead code and pull in scipy and
# matplotlib -- see WP10).
ROOT_MODULES = ('main', 'AI_Control')

# Modules that must never be imported at module level again.
PLATFORM_MODULES = {'pyautogui', 'winsound', 'pynput', 'pygame', 'tkinter'}

# Files that legitimately talk to a platform module directly: the shim itself
# and the ctypes binding it hands out.
SHIM_EXEMPT = {
    os.path.join('misc', 'platform_shim.py'),
    os.path.join('misc', 'vjoy.py'),
}


def _app_modules():
    modules = list(ROOT_MODULES)
    for package in PACKAGES:
        for info in pkgutil.iter_modules([os.path.join(PROJECT_ROOT, package)]):
            modules.append(f"{package}.{info.name}")
    return sorted(modules)


def _python_files():
    for package in PACKAGES:
        directory = os.path.join(PROJECT_ROOT, package)
        for name in sorted(os.listdir(directory)):
            if name.endswith('.py'):
                yield os.path.join(package, name)
    for name in ROOT_MODULES:
        yield f"{name}.py"


@pytest.mark.parametrize("module_name", _app_modules())
def test_module_imports_without_windows(module_name):
    importlib.import_module(module_name)


def test_main_imports():
    """`python -c "import main"` must get as far as needing a socket."""
    import main

    assert hasattr(main, 'LFSAssistantApp')


@pytest.mark.parametrize("relative_path", list(_python_files()))
def test_no_platform_import_at_module_level(relative_path):
    """Windows-only modules go through misc/platform_shim.py, nowhere else."""
    if relative_path in SHIM_EXEMPT:
        pytest.skip("the shim itself")

    with open(os.path.join(PROJECT_ROOT, relative_path), encoding='utf-8') as handle:
        tree = ast.parse(handle.read(), filename=relative_path)

    offenders = []
    for node in tree.body:  # module level only; a lazy import in a function is fine
        if isinstance(node, ast.Import):
            offenders += [alias.name.split('.')[0] for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            offenders.append(node.module.split('.')[0])

    assert not (PLATFORM_MODULES & set(offenders)), (
        f"{relative_path} imports a platform module at module level; "
        f"use misc.platform_shim instead")
