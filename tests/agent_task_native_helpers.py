"""测试从真实工作区调用会话SDK；不注册退役模型工具。"""
import ast
import json


def task_call(arguments, *, wire=False):
    arguments = dict(arguments)
    method = {"create": "start_task", "revise": "revise_task", "checkpoint": "checkpoint_task", "cancel": "cancel_task"}[arguments.pop("operation")]
    return "agent__run_code" if wire else "agent.run_code", {"code": (
        "import json\nfrom mf import session\n"
        f"print(json.dumps(session.{method}(**{arguments!r}), ensure_ascii=False))"
    )}


def task_function(arguments):
    name, args = task_call(arguments, wire=True)
    return {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}


def returned_task(value):
    if value.get("task"):
        return value["task"]
    stdout = value.get("data", {}).get("stdout", "")
    for line in reversed(stdout.splitlines()):
        try:
            result = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(result, dict) and result.get("data", {}).get("task"):
            return result["data"]["task"]
    return None


def task_values(function):
    code = json.loads(function["arguments"])["code"]
    for node in ast.walk(ast.parse(code)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) and node.func.value.id == "session":
            values = ast.literal_eval(node.keywords[0].value)
            values["operation"] = {"start_task": "create", "revise_task": "revise", "checkpoint_task": "checkpoint", "cancel_task": "cancel"}[node.func.attr]
            return values
    raise AssertionError("fixture did not call a native task method")
