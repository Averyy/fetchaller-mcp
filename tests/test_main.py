"""CLI entrypoint behavior."""

import os
import subprocess
import sys
from pathlib import Path

from fetchaller import main as main_module


def test_keyboard_interrupt_is_a_clean_operator_shutdown(monkeypatch):
    config = object()
    sentinel = object()
    monkeypatch.setattr(sys, "argv", ["fetchaller-mcp"])
    monkeypatch.setattr(main_module, "load_config", lambda: config)
    monkeypatch.setattr(main_module, "run_stdio_mode", lambda value: sentinel)

    def interrupted(value):
        assert value is sentinel
        raise KeyboardInterrupt

    monkeypatch.setattr(main_module.asyncio, "run", interrupted)

    main_module.main()


def test_entrypoint_account_setup_never_contaminates_mcp_stdout(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    commands = {
        "id": """#!/bin/sh
if [ "$1" = "-u" ] && [ "$#" -eq 1 ]; then echo 0
elif [ "$1" = "-u" ]; then echo 99
elif [ "$1" = "-g" ]; then echo 100
else exit 1
fi
""",
        "usermod": "#!/bin/sh\necho 'usermod: no changes'\n",
        "groupmod": "#!/bin/sh\necho 'groupmod: no changes'\n",
        "chown": "#!/bin/sh\nexit 0\n",
        "gosu": "#!/bin/sh\nexit 0\n",
    }
    for name, body in commands.items():
        path = fake_bin / name
        path.write_text(body)
        path.chmod(0o755)

    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        ["/bin/sh", str(root / "entrypoint.sh"), "ignored-command"],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "PUID": "501",
            "PGID": "20",
        },
        check=False,
        timeout=10,
    )

    assert result.returncode == 0
    assert result.stdout == ""


def test_entrypoint_supervisor_explicitly_preserves_stdio_stdin():
    root = Path(__file__).resolve().parents[1]
    entrypoint = (root / "entrypoint.sh").read_text()

    assert "exec 3<&0" in entrypoint
    assert '"$@" <&3 &' in entrypoint
    assert "exec 3<&-" in entrypoint
    assert "eval " not in entrypoint


def test_entrypoint_rejects_invalid_or_root_runtime_identity(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_id = fake_bin / "id"
    fake_id.write_text("#!/bin/sh\necho 0\n")
    fake_id.chmod(0o755)
    root = Path(__file__).resolve().parents[1]

    for puid, pgid in (
        ("0", "100"),
        ("00", "100"),
        ("000", "100"),
        ("99", "0"),
        ("99", "00"),
        ("not-a-number", "100"),
        ("99", "not-a-number"),
        ("2147483648", "100"),
        ("99", "2147483648"),
    ):
        result = subprocess.run(
            ["/bin/sh", str(root / "entrypoint.sh"), "ignored-command"],
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "PATH": f"{fake_bin}:/usr/bin:/bin",
                "PUID": puid,
                "PGID": pgid,
            },
            check=False,
            timeout=10,
        )

        assert result.returncode == 1
        assert result.stdout == ""
        assert "PUID and PGID must be" in result.stderr


def test_entrypoint_rejects_invalid_timeouts_before_display_start(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_id = fake_bin / "id"
    fake_id.write_text("#!/bin/sh\necho 1000\n")
    fake_id.chmod(0o755)
    root = Path(__file__).resolve().parents[1]
    side_effect = tmp_path / "must-not-exist"

    for startup, shutdown in (
        ("0", "60"),
        ("00", "60"),
        ("180", "00"),
        ("3601", "60"),
        ("180", "3601"),
        (f"$(touch {side_effect})", "60"),
    ):
        result = subprocess.run(
            ["/bin/sh", str(root / "entrypoint.sh"), "ignored-command"],
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "PATH": f"{fake_bin}:/usr/bin:/bin",
                "HTTP_STARTUP_TIMEOUT": startup,
                "APP_SHUTDOWN_TIMEOUT": shutdown,
            },
            check=False,
            timeout=10,
        )

        assert result.returncode == 1
        assert result.stdout == ""
        assert "HTTP_STARTUP_TIMEOUT and APP_SHUTDOWN_TIMEOUT" in result.stderr
        assert not side_effect.exists()
