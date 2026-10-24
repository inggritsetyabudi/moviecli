import os
import subprocess
import sys

import pytest


def run_system_command(command: str) -> int:
    try:
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        if sys.platform == "win32":
            import re
            command = re.sub(r"'([^']*)'", r'"\1"', command)
        result = subprocess.run(
            f'"{sys.executable}" -m moviebox_api ' + command,
            shell=True,
            check=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            env=env,
        )
        return result.returncode
    except subprocess.CalledProcessError as e:
        print(e, e.output, sep="\n")
        return e.returncode


def test_version():
    returncode = run_system_command("--version")
    assert returncode <= 0


@pytest.mark.parametrize(
    argnames=[
        "command",
    ],
    argvalues=[
        ["download-movie --help"],
        ["download-series --help"],
        ["download-anime --help"],
        ["source-anime --help"],
        ["mirror-hosts --help"],
        ["homepage-content --help"],
        ["popular-search --help"],
        ["item-details --help"],
        ["interactive --help"],
        ["interactive-tui --help"],
        ["secret-set --help"],
        ["secret-unset --help"],
        ["secret-status --help"],
        ["source-streams --help"],
        ["vega-providers --help"],
    ],
)
def test_help(command):
    returncode = run_system_command(command)
    assert returncode <= 0


@pytest.mark.parametrize(
    argnames=[
        "command",
    ],
    argvalues=[
        ["download-movie avatar -YT"],
        ["download-series merlin -s 1 -e 1 -YT"],
    ],
)
def test_download(command):
    returncode = run_system_command(command)
    assert returncode <= 0


def test_mirror_hosts():
    returncode = run_system_command("mirror-hosts --json")
    assert returncode <= 0


@pytest.mark.parametrize(
    argnames=[
        "command",
    ],
    argvalues=[
        ["homepage-content"],
        ["homepage-content --json"],
        ["homepage-content --banner"],
        ["homepage-content --banner --json"],
        ["homepage-content --title 'Trending Now🔥'"],
    ],
)
def test_homepage(command):
    returncode = run_system_command(command)
    assert returncode <= 0


@pytest.mark.parametrize(
    argnames=[
        "command",
    ],
    argvalues=(["popular-search"], ["popular-search --json"]),
)
def test_popular_search(command):
    returncode = run_system_command(command)
    assert returncode <= 0


@pytest.mark.parametrize(
    argnames=[
        "command",
    ],
    argvalues=(["item-details Merlin --yes --json"], ["item-details Avatar -s MOVIES -Y -F"]),
)
def test_item_details(command):
    returncode = run_system_command(command)
    assert returncode <= 0
