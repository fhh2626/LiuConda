import ast
import hashlib
import importlib
import importlib.util
import inspect
import json
import os
import pickle
import re
import shutil
import sys
import tempfile
import threading
import time
import traceback
from typing import Any, Dict, List, Tuple


JsonDict = Dict[str, Any]
AliasEntry = Tuple[int, str, str]


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--server":
        server_main(parent_pid())
        return

    action = sys.argv[1] if len(sys.argv) > 1 else ""
    payload = json.loads(sys.stdin.read() or "{}")

    try:
        import jedi

        write(run_action_with_cache(jedi, action, payload))
    except Exception as error:
        write(
            {
                "error": str(error),
                "traceback": traceback.format_exc(),
            }
        )


def parent_pid() -> int:
    try:
        index = sys.argv.index("--parent-pid")
        return int(sys.argv[index + 1])
    except Exception:
        return 0


def server_main(parent: int = 0) -> None:
    start_parent_watchdog(parent)
    try:
        import jedi
    except Exception as error:
        write_line({"id": None, "error": str(error), "traceback": traceback.format_exc()})
        return

    cache_dir = jedi_cache_directory()
    jedi.settings.cache_directory = cache_dir

    for line in sys.stdin:
        if not line.strip():
            continue

        try:
            request = json.loads(line)
        except Exception as error:
            write_line({"id": None, "error": str(error), "traceback": traceback.format_exc()})
            continue

        request_id = request.get("id")
        action = request.get("action", "")
        if action == "shutdown":
            write_line({"id": request_id, "result": {"ok": True}})
            return

        payload = request.get("payload", {})
        try:
            result = run_action_with_recovery(jedi, action, payload, cache_dir, use_lock=True)
            write_line({"id": request_id, "result": result})
        except Exception as error:
            write_line(
                {
                    "id": request_id,
                    "error": str(error),
                    "traceback": traceback.format_exc(),
                }
            )


def start_parent_watchdog(parent: int) -> None:
    if not parent:
        return

    def watch() -> None:
        unknown_count = 0
        while True:
            time.sleep(2)
            alive = process_is_alive(parent)
            if alive is False:
                os._exit(0)
            if alive is None:
                unknown_count += 1
                if unknown_count >= 5:
                    os._exit(0)
            else:
                unknown_count = 0

    threading.Thread(target=watch, daemon=True).start()


def process_is_alive(pid: int) -> bool | None:
    if not pid:
        return False

    if os.name == "nt":
        return _windows_process_is_alive(pid)

    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _windows_process_is_alive(pid: int) -> bool | None:
    try:
        import ctypes

        synchronize = 0x00100000
        wait_timeout = 0x00000102
        wait_failed = 0xFFFFFFFF
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(synchronize, False, int(pid))
        if not handle:
            return False
        try:
            result = kernel32.WaitForSingleObject(handle, 0)
            if result == wait_timeout:
                return True
            if result == wait_failed:
                return None
            return False
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return None


def run_action_with_cache(jedi: Any, action: str, payload: JsonDict) -> JsonDict:
    return run_action_with_recovery(jedi, action, payload, jedi_cache_directory(), use_lock=True)


def run_action_with_recovery(
    jedi: Any,
    action: str,
    payload: JsonDict,
    cache_dir: str,
    use_lock: bool = False,
) -> JsonDict:
    lock_path = os.path.join(cache_dir, ".lock") if use_lock else ""
    for attempt in range(2):
        try:
            if lock_path:
                acquire_cache_lock(lock_path)
            jedi.settings.cache_directory = cache_dir
            return run_action(jedi, action, payload)
        except (EOFError, pickle.UnpicklingError, ValueError):
            if attempt == 0:
                clear_cache_directory(cache_dir)
                continue
            raise
        except OSError as error:
            if isinstance(error, TimeoutError):
                raise
            if attempt == 0:
                clear_cache_directory(cache_dir)
                continue
            raise
        finally:
            if lock_path:
                remove_cache_lock(lock_path)


def run_action(jedi: Any, action: str, payload: JsonDict) -> JsonDict:
    source = payload.get("source", "")
    script = jedi.Script(
        code=source,
        path=payload.get("path") or None,
    )
    line = int(payload.get("line", 1))
    column = int(payload.get("column", 0))

    if action == "complete":
        return {"completions": complete(script, line, column)}
    if action == "goto":
        return {"definitions": goto(script, line, column)}
    if action == "signature":
        return {"signatures": signatures(script, line, column)}
    if action == "documentation":
        return {
            "items": documentation(
                script,
                source,
                line,
                column,
                payload.get("runtime_import_whitelist") or [],
            )
        }
    return {"error": "Unknown action: {}".format(action)}


def jedi_cache_directory() -> str:
    key = hashlib.sha1(sys.executable.encode("utf-8")).hexdigest()[:16]
    path = os.path.join(tempfile.gettempdir(), "LiuCondaJediCache", key)
    os.makedirs(path, exist_ok=True)
    return path


def acquire_cache_lock(lock_path: str) -> None:
    deadline = time.time() + 10
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, str(time.time()).encode("utf-8"))
            except Exception:
                os.close(fd)
                remove_cache_lock(lock_path)
                raise
            os.close(fd)
            return
        except FileExistsError:
            if time.time() > deadline:
                if _lock_is_stale(lock_path):
                    remove_cache_lock(lock_path)
                    deadline = time.time() + 10
                    continue
                raise TimeoutError("Cache lock timeout")
            time.sleep(0.05)


def _lock_is_stale(lock_path: str) -> bool:
    try:
        age = time.time() - os.path.getmtime(lock_path)
        return age > 30
    except OSError:
        return True


def remove_cache_lock(lock_path: str) -> None:
    try:
        os.unlink(lock_path)
    except OSError:
        pass


def clear_cache_directory(cache_dir: str) -> None:
    lock_name = ".lock"
    for name in os.listdir(cache_dir):
        if name == lock_name:
            continue
        path = os.path.join(cache_dir, name)
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        else:
            try:
                os.unlink(path)
            except OSError:
                pass


def complete(script: Any, line: int, column: int) -> List[JsonDict]:
    items: List[JsonDict] = []
    for item in script.complete(line, column):
        name = item.name
        item_type = getattr(item, "type", "")
        trigger = "{}\t{}".format(name, item_type) if item_type else name
        items.append(
            {
                "name": name,
                "trigger": trigger,
                "completion": name,
                "type": item_type,
            }
        )
    return items


def goto(script: Any, line: int, column: int) -> List[JsonDict]:
    names = script.goto(line, column, follow_imports=True, follow_builtin_imports=True)
    if not names:
        names = script.infer(line, column)

    items: List[JsonDict] = []
    for name in names:
        path = str(name.module_path) if name.module_path else ""
        items.append(
            {
                "name": name.name,
                "type": getattr(name, "type", ""),
                "path": path,
                "line": name.line,
                "column": name.column,
            }
        )
    return items


def signatures(script: Any, line: int, column: int) -> List[JsonDict]:
    items: List[JsonDict] = []
    for signature in script.get_signatures(line, column):
        params = [param.to_string() for param in signature.params]
        items.append(
            {
                "name": signature.name,
                "params": params,
                "index": getattr(signature, "index", None),
                "docstring": signature.docstring(),
            }
        )
    return items


def documentation(
    script: Any,
    source: str,
    line: int,
    column: int,
    runtime_import_whitelist: List[str],
) -> List[JsonDict]:
    names = script.help(line, column)
    if not names:
        names = script.infer(line, column)

    items: List[JsonDict] = []
    for name in names:
        path = str(name.module_path) if name.module_path else ""
        items.append(
            {
                "name": name.full_name or name.name,
                "type": getattr(name, "type", ""),
                "path": path,
                "docstring": name.docstring(),
            }
        )
    if items:
        return items

    runtime_item = runtime_documentation(
        source,
        line,
        column,
        runtime_import_whitelist,
    )
    if runtime_item:
        items.append(runtime_item)
    return items


def runtime_documentation(
    source: str,
    line: int,
    column: int,
    runtime_import_whitelist: List[str],
) -> JsonDict:
    expression = dotted_expression_at(source, line, column)
    if not expression:
        return {}

    target_parts = imported_target_parts(source, expression, line)
    if not target_parts:
        return {}
    if not import_allowed(target_parts, runtime_import_whitelist):
        return {}

    resolved = resolve_imported_object(target_parts)
    if not resolved:
        return {}

    full_name, obj = resolved
    docstring = inspect.getdoc(obj) or ""
    if not docstring:
        return {}

    return {
        "name": full_name,
        "type": type(obj).__name__,
        "path": inspect_object_path(obj),
        "docstring": docstring,
    }


def import_allowed(target_parts: List[str], whitelist: List[str]) -> bool:
    dotted = ".".join(target_parts)
    for item in whitelist:
        allowed = str(item).strip()
        if allowed and (dotted == allowed or dotted.startswith(allowed + ".")):
            return True
    return False


def dotted_expression_at(source: str, line: int, column: int) -> str:
    rows = source.splitlines()
    if line < 1 or line > len(rows):
        return ""

    row = rows[line - 1]
    if not row:
        return ""

    pos = min(max(column, 0), len(row))
    if pos == len(row) or not dotted_name_char(row[pos]):
        if pos == 0 or not dotted_name_char(row[pos - 1]):
            return ""
        pos -= 1

    start = pos
    while start > 0 and dotted_name_char(row[start - 1]):
        start -= 1

    end = pos + 1
    while end < len(row) and dotted_name_char(row[end]):
        end += 1

    expression = row[start:end].strip(".")
    if re.match(r"^[A-Za-z_]\w*(\.[A-Za-z_]\w*)*$", expression):
        return expression
    return ""


def dotted_name_char(char: str) -> bool:
    return char == "." or char == "_" or char.isalnum()


def imported_target_parts(
    source: str,
    expression: str,
    line: int,
) -> List[str]:
    parts = expression.split(".")
    if not parts:
        return []

    aliases = import_aliases(source, line)
    target = aliases.get(parts[0])
    if not target:
        return []

    return target.split(".") + parts[1:]


def import_aliases(source: str, line: int) -> Dict[str, str]:
    entries = import_alias_entries(source)
    aliases: Dict[str, str] = {}
    for node_line, local_name, target in entries:
        if node_line and node_line > line:
            continue
        aliases[local_name] = target
    return aliases


def import_alias_entries(source: str) -> List[AliasEntry]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    return collect_import_alias_entries(tree)


def collect_import_alias_entries(tree: ast.AST) -> List[AliasEntry]:
    entries: List[AliasEntry] = []

    class ImportAliasVisitor(ast.NodeVisitor):
        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            return None

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            return None

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            return None

        def visit_Lambda(self, node: ast.Lambda) -> None:
            return None

        def visit_Import(self, node: ast.Import) -> None:
            node_line = int(getattr(node, "lineno", 0) or 0)
            for alias in node.names:
                if alias.asname:
                    entries.append((node_line, alias.asname, alias.name))
                else:
                    root = alias.name.split(".", 1)[0]
                    entries.append((node_line, root, root))

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            if not node.module or node.level != 0:
                return
            node_line = int(getattr(node, "lineno", 0) or 0)
            for alias in node.names:
                if alias.name == "*":
                    continue
                local_name = alias.asname or alias.name
                entries.append((node_line, local_name, "{}.{}".format(node.module, alias.name)))

    ImportAliasVisitor().visit(tree)

    entries.sort(key=lambda item: item[0])
    return entries


def resolve_imported_object(parts: List[str]) -> Tuple[str, Any] | None:
    best = None
    for split in range(len(parts), 0, -1):
        module_name = ".".join(parts[:split])
        if importlib.util.find_spec(module_name) is None:
            continue
        try:
            obj = importlib.import_module(module_name)
        except (ImportError, ModuleNotFoundError):
            continue

        full_parts = parts[:split]
        try:
            for attr in parts[split:]:
                obj = getattr(obj, attr)
                full_parts.append(attr)
        except AttributeError:
            if best is None:
                best = (".".join(full_parts), obj)
            continue

        return (".".join(full_parts), obj)

    return best


def inspect_object_path(obj: Any) -> str:
    try:
        path = inspect.getsourcefile(obj) or inspect.getfile(obj)
    except (TypeError, OSError):
        path = ""
    return path or ""


def write(data: JsonDict) -> None:
    sys.stdout.write(json.dumps(data))
    sys.stdout.flush()


def write_line(data: JsonDict) -> None:
    sys.stdout.write(json.dumps(data) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
