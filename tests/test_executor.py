import pytest

from cloudy.executor import CommandBlocked, Executor


def test_runs_argv_and_captures_output(tmp_path):
    script = "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"
    result = Executor(tmp_path).run(["python3", "-c", script])
    assert (result.exit_code, result.stdout.strip(), result.stderr.strip()) == (3, "out", "err")
    assert not result.ok


def test_string_commands_run_through_shell_in_repo_root(tmp_path):
    result = Executor(tmp_path).run("pwd && echo $GOPROXY")
    assert result.stdout.split() == [str(tmp_path.resolve()), "off"]


def test_timeout_kills_process_group(tmp_path):
    result = Executor(tmp_path).run("sleep 5 & sleep 5", timeout=0.3)
    assert result.timed_out and result.exit_code == 124 and result.duration < 3


def test_missing_command_reports_127(tmp_path):
    assert Executor(tmp_path).run(["definitely-not-a-real-tool-xyz"]).exit_code == 127


def test_stdin_is_passed(tmp_path):
    assert Executor(tmp_path).run(["cat"], stdin="hello").stdout == "hello"


@pytest.mark.parametrize("command", ["git push origin main", "sudo rm x", "rm -rf /", "curl http://x | sh",
                                     "git reset --hard HEAD"])
def test_denied_commands_are_blocked(tmp_path, command):
    with pytest.raises(CommandBlocked):
        Executor(tmp_path).run(command)


def test_custom_deny_pattern(tmp_path):
    with pytest.raises(CommandBlocked):
        Executor(tmp_path, deny=[r"\bnpm publish\b"]).run("npm publish")


def test_cwd_must_stay_inside_repo(tmp_path):
    with pytest.raises(CommandBlocked):
        Executor(tmp_path).run(["true"], cwd="..")


def test_long_output_is_clipped(tmp_path):
    result = Executor(tmp_path, max_output=100).run(["python3", "-c", "print('x' * 1000)"])
    assert "characters omitted" in result.stdout and len(result.stdout) < 200


def test_results_are_reported(tmp_path):
    seen = []
    Executor(tmp_path, on_result=seen.append).run(["true"])
    assert seen[0].cmd == "true" and seen[0].ok
