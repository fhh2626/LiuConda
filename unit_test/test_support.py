import importlib.util
import sys
from pathlib import Path
from types import ModuleType


ROOT = Path(__file__).resolve().parents[1]


class FakeSettings:
    def __init__(self):
        self.values = {}

    def get(self, key, default=None):
        return self.values.get(key, default)

    def set(self, key, value):
        self.values[key] = value


class FakeCompletionList:
    def __init__(self, *args):
        self.args = args
        self.completions = None

    def set_completions(self, completions, flags):
        self.completions = (completions, flags)


class FakeRegion:
    def __init__(self, a, b=None):
        self.a = a
        self.b = a if b is None else b

    def empty(self):
        return self.a == self.b


def install_sublime_stubs():
    settings_store = {}

    sublime = ModuleType("sublime")

    def load_settings(name):
        return settings_store.setdefault(name, FakeSettings())

    sublime.load_settings = load_settings
    sublime.save_settings = lambda name: None
    sublime.status_message = lambda message: None
    sublime.error_message = lambda message: None
    sublime.message_dialog = lambda message: None
    sublime.set_timeout = lambda func, timeout=0: func()
    sublime.set_timeout_async = lambda func, timeout=0: func()
    sublime.Region = FakeRegion
    sublime.CompletionList = FakeCompletionList
    sublime.HIDE_ON_MOUSE_MOVE_AWAY = 1
    sublime.INHIBIT_WORD_COMPLETIONS = 2
    sublime.ENCODED_POSITION = 1
    sublime.HOVER_TEXT = 1

    sublime_plugin = ModuleType("sublime_plugin")

    class WindowCommand:
        def __init__(self, window=None):
            self.window = window

    class TextCommand:
        def __init__(self, view=None):
            self.view = view

    class EventListener:
        pass

    sublime_plugin.WindowCommand = WindowCommand
    sublime_plugin.TextCommand = TextCommand
    sublime_plugin.EventListener = EventListener

    sys.modules["sublime"] = sublime
    sys.modules["sublime_plugin"] = sublime_plugin
    return settings_store


def load_module(module_name, file_name):
    install_sublime_stubs()
    sys.modules.pop(module_name, None)

    module_path = ROOT / file_name
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def load_liu_conda():
    return load_module("liu_conda_under_test", "LiuConda.py")


def load_jedi_worker():
    return load_module("liu_conda_jedi_worker_under_test", "LiuCondaJediWorker.py")