"""复制为工作区中的 mf.py；仅依赖标准库，经本次执行的私有请求文件 调用 SDK。"""
from __future__ import annotations

import argparse
import inspect
import json
import linecache
import os
import stat
import sys
import time
import uuid
from pathlib import Path
from types import ModuleType
from typing import Any, get_args, get_origin


class Error(RuntimeError):
    """业务服务返回的失败；代码可捕获、检查 code 并正常重试。"""

    def __init__(self, message, *, code="error"):
        super().__init__(f"{code}: {message}")
        self.code = code


def _request(method, arguments):
    arguments = {key: value for key, value in arguments.items() if value is not Ellipsis}
    payload = json.dumps({"method": method, "arguments": arguments},
                         ensure_ascii=False, allow_nan=False).encode()
    if len(payload) > 4 * 1024 * 1024:
        raise Error("request exceeds 4 MiB", code="request_too_large")
    identity = uuid.uuid4().hex
    name, temporary = identity + ".json", "." + identity
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    requests = os.open("/run/mf-requests", flags)
    responses = os.open("/run/mf-responses", flags)
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=requests)
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
        os.replace(temporary, name, src_dir_fd=requests, dst_dir_fd=requests)
        while True:
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=responses)
            except FileNotFoundError:
                time.sleep(0.020)
                continue
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.stat("/opt/mf").st_uid or info.st_nlink != 1:
                    raise Error("invalid response file", code="invalid_response")
                reply = json.load(stream)
            if reply.get("error"):
                raise Error(reply["error"]["message"], code=reply["error"]["code"])
            return reply["result"]
    finally:
        for item in (temporary, name):
            try:
                os.unlink(item, dir_fd=requests)
            except FileNotFoundError:
                pass
        os.close(requests)
        os.close(responses)



def _install_methods():
    methods = {}
    for spec in json.loads(Path(__file__).with_name("mf-methods.json").read_text()):
        namespace, name = spec["name"].split(".")
        module = globals().setdefault(namespace, ModuleType(f"{__name__}.{namespace}", spec["namespace_doc"]))
        sys.modules[module.__name__] = module
        # 签名来自服务端自己的函数定义；工作区请求不能新增导出方法。
        scope = {"_request": _request, "ellipsis": type(Ellipsis)}
        source = f"def {name}{spec['signature']}:\n    {spec['doc']!r}\n    return _request({spec['name']!r}, locals())\n"
        filename = f"<{__name__}:{spec['name']}>"
        linecache.cache[filename] = (len(source), None, source.splitlines(keepends=True), filename)
        exec(compile(source, filename, "exec", dont_inherit=True), scope)
        # 延迟注解仍从scope解析内置类型；不能让生成的list等方法遮蔽它们。
        function = scope.pop(name)
        function.__module__ = module.__name__
        setattr(module, name, function)
        methods[spec["name"]] = function
    return methods


_METHODS = _install_methods()
# 导出表来自宿主生成的 SDK 清单；动态命名空间不能由静态分析器展开。
# pyrefly: ignore [unresolvable-dunder-all]
__all__ = ["Error", *sorted({name.split(".")[0] for name in _METHODS})]
__doc__ = "MediaFlux 业务接口。mf --help 查看命令，help(mf.<模块>) 查看方法。\n\n" + "\n".join(
    f"{name}: {globals()[name].__doc__}" for name in __all__ if name != "Error")


def __dir__():
    return __all__


def _json_argument(value):
    return json.loads(Path(value[1:]).read_text() if value.startswith("@") else value)


def _json_or_text(value):
    return _json_argument(value) if value.startswith("@") or value.lstrip().startswith(("[", "{")) else value


def _integer_or_text(value: str) -> int | str:
    try:
        return int(value)
    except ValueError:
        return value


class _StringListArgument(argparse.Action):
    """字符串集合沿用逐项参数，也接受一个显式 @JSON 文件。"""

    def __call__(self, parser, namespace, values, option_string=None):
        if len(values) == 1 and values[0].startswith("@"):
            try:
                values = _json_argument(values[0])
            except (OSError, ValueError) as exc:
                raise argparse.ArgumentError(self, str(exc)) from exc
            if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
                raise argparse.ArgumentError(self, "JSON 文件必须是字符串数组")
        setattr(namespace, self.dest, values)


def main():
    parser = argparse.ArgumentParser(prog="mf", description=__doc__,
        epilog="mf <模块> --help 查看该模块的方法；mf <模块.方法> --help 查看完整参数。复杂 JSON 参数可用 @文件.json。",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="method", required=True, metavar="<模块.方法>")
    for namespace in __all__:
        if namespace == "Error":
            continue
        section = commands.add_parser(namespace, description=globals()[namespace].__doc__,
            epilog="\n".join(f"{name}: {(function.__doc__ or '').split(chr(10), 1)[0].split('。', 1)[0]}"
                             for name, function in _METHODS.items() if name.startswith(namespace + "."))
                   + "\n\n使用 mf <模块.方法> --help 查看完整参数。",
            formatter_class=argparse.RawDescriptionHelpFormatter)
        section.set_defaults(namespace_help=True)
    for name, function in _METHODS.items():
        command = commands.add_parser(name,
            description=inspect.cleandoc(function.__doc__ or ""), formatter_class=argparse.RawDescriptionHelpFormatter)
        for parameter in inspect.signature(function).parameters.values():
            annotation = inspect.formatannotation(parameter.annotation)
            members = get_args(parameter.annotation)
            options: dict[str, Any] = {"dest": parameter.name}
            if parameter.default is inspect.Parameter.empty:
                options["required"] = True
            else:
                options["default"] = parameter.default
            if annotation.startswith("bool"):
                options["action"] = argparse.BooleanOptionalAction
            elif str in members and any(get_origin(member) in {list, dict} for member in members):
                options["type"] = _json_or_text
            elif set(members) == {str, int}:
                options["type"] = _integer_or_text
            elif annotation.startswith("list[str]"):
                options.update(action=_StringListArgument, nargs="*", help="逐项传入字符串，或 @JSON文件")
            elif annotation.startswith(("dict", "list[")):
                options["type"] = _json_argument
            else:
                options["type"] = int if annotation.startswith("int") else float if annotation.startswith("float") else str
            command.add_argument("--" + parameter.name.replace("_", "-"), **options)
    arguments = vars(parser.parse_args())
    method = arguments.pop("method")
    if arguments.pop("namespace_help", False):
        commands.choices[method].print_help()
        return
    try:
        print(json.dumps(_METHODS[method](**arguments), ensure_ascii=False, indent=2))
    except Error as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
